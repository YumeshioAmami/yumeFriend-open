"""给助手回复配音（v2.0 / T2.0-5；决策见 D85 / D86 / D88）。

两条路：

A. **整段合成**（`schedule()`）：主动消息、提醒、手动「念给我听」走这条——拿到完整文字，
   一次合成。（老路，行为不变。）

B. **边流边合成**（`open_stream()` / `StreamingVoice`，D88 "Level 1"）：聊天回复**还在流**
   的时候，**一句说完了就把那句先合成掉**（要求句末标点 + 括号闭合，遵守 D86 不念括号）；
   回复结束后把几句**拼成一条 wav** 再标 `ready`。
   因为 LLM 在云端、本地显卡闲着，这段合成基本不额外占时间 —— "打完字还要干等一遍语音"
   的尾部空等就没了。**前端和数据结构都不用改**。

四条口径（都来自 D85）：

1. **不阻塞文字**：合成在后台线程；失败只影响语音，文字一个字不动。
2. **每条都能回放**：按消息存 `user_data/audio/<message_id>.wav`（不是只靠合成缓存）。
3. **失败不是错误**：`audio_status='failed'` + `audio_error` 一句人话。
4. **不需要问模型**：发不发语音由开关决定，不是提示词的事。
"""

from __future__ import annotations

import asyncio
import io
import json
import os
import re
import shutil
import time
import wave
from pathlib import Path
from types import SimpleNamespace

from loguru import logger
from sqlalchemy import select

from backend import config
from backend.api.ws import manager
from backend.db import session_scope
from backend.models import Friend, Message, Voice
from backend.services import settings_service, tts_service
from backend.services.errors import ServiceError

PENDING = "pending"
READY = "ready"
FAILED = "failed"

# 正在跑的任务（按 message_id）：同一条别重复排队
_running: dict[str, asyncio.Task] = {}  # type: ignore[type-arg]

# 主事件循环（lifespan 启动时绑定）。同步端点（如 /speak）跑在 FastAPI 线程池里，
# 那里 `get_running_loop()` 会抛异常 —— 得靠 `call_soon_threadsafe` 把活儿丢回主循环。
# （和 `llm/warmup.py` 的 `bind_loop` 是同一个套路。）
_MAIN_LOOP: asyncio.AbstractEventLoop | None = None

# v3.11（D178）：**正在流式写盘**的分段（`message_id:index`）。
# HTTP 端点据此决定"尾随这个还在长大的文件"还是"直接发完整文件"。
_streaming: set[str] = set()


def _stream_key(message_id: str, index: int) -> str:
    return f"{message_id}:{index}"


def is_streaming(message_id: str, index: int) -> bool:
    return _stream_key(message_id, index) in _streaming

# D107：一条回复的语音**总时长上限**。到点就"能听多少听多少"。
# v3.12（D180）：合成 ≈ 1:1 实时 + 用户常有"一分钟左右的音频" → 120 秒太紧（他实测：
# "还没合成完就判定失败"）→ 抬到 **5 分钟**（用户原话："两分钟都不为过"）。
VOICE_TOTAL_TIMEOUT_SECONDS = 300.0

# 边流边合成：一句话可念字数到多少才值得单独切（D88）
MIN_SEGMENT_CHARS = 6
# 句末标点（`…` 不算：中文 RP 里"……"常常是句中的停顿，切了会碎成一地）
_SENTENCE_END = "。！？!?；;\n"

# v2.2（D105）：**段** = 一个自然段（空行分段）。一段可念字数超过这个数，
# 就在句末切开 —— 免得一个超长自然段把"第一段音频"拖到天荒地老。
MAX_SEGMENT_CHARS = 90
# 段落之间的空行（允许空格缩进）
_PARA_BREAK = re.compile(r"\n[ \t]*\n[ \t\n]*")

# 合成前把 markdown 记号清掉——TTS 会把 `**` 念成"星星"、`#` 念成"井号"
_MD_LINK = re.compile(r"\[([^\]]*)\]\([^)]*\)")
# 括号里的是**演**不是**说**（动作 / 心理描写），不念（D86）
_PARENS = re.compile(r"[（(].*?[）)]")


def bind_loop(loop: asyncio.AbstractEventLoop) -> None:
    global _MAIN_LOOP
    _MAIN_LOOP = loop


def _active_loop() -> asyncio.AbstractEventLoop | None:
    try:
        return asyncio.get_running_loop()
    except RuntimeError:
        if _MAIN_LOOP is None or _MAIN_LOOP.is_closed():
            return None
        return _MAIN_LOOP


# ---------- 文本 ----------


def _strip_mood_marks(text: str) -> str:
    from backend.agent.parser import strip_mood_marks  # 懒导入，避免循环

    return strip_mood_marks(text)


def speakable_text(text: str) -> str:
    """**要念出来的文本**（不等于消息正文，D86）。

    1. markdown 链接只留文字、记号清掉（`**` / `#` / 反引号）
    2. **括号内容整段剔掉**：`（笑）你好呀` → `你好呀`（那是动作 / 心理描写，不是台词）
    3. **v3.11（D178）洗字符**：emoji 之类"GBK 编不出来"的字符全去掉 ——
       上游在 Windows 上按 GBK 处理文本，碰到 emoji 直接 400，**整段就废了**
       （实测：`'gbk' codec can't encode character '\U0001f60f'`）

    正文一个字都不动——剔的只是"念什么"。
    """
    # D113：先摘掉情绪标签（`**生气**：` / `[立绘:高兴]`）—— 别让语音念出"生气："这种出戏的字
    cleaned = _strip_mood_marks(text or "")
    cleaned = _MD_LINK.sub(r"\1", cleaned)
    cleaned = _PARENS.sub("", cleaned)
    cleaned = re.sub(r"[*`#]+", "", cleaned)
    cleaned = re.sub(r"^\s*>\s?", "", cleaned, flags=re.MULTILINE)
    cleaned = re.sub(r"\n{2,}", "\n", cleaned)
    cleaned = _gbk_safe(cleaned)
    # 去掉 emoji 之后可能留下"两个空格贴一起" → 收一收
    cleaned = re.sub(r"[ \t]{2,}", " ", cleaned)
    return cleaned.strip()


def _gbk_safe(text: str) -> str:
    """只留**GBK 能编出来**的字符（汉字 / 标点 / 英数 / 假名…），emoji 和生僻符号全去掉。

    为什么不上白名单正则：GBK 支持哪些字符（含 CJK 扩展、假名、全角标点）用 `encode` 一试就知道，
    比自己维护一张表准得多。**代价**：极少数生僻字（如 GBK 之外的字）念不出来 —— 去掉比整段失败划算。
    """
    out: list[str] = []
    for char in text:
        try:
            char.encode("gbk")
        except UnicodeEncodeError:
            continue
        out.append(char)
    return "".join(out)


def _paren_depth(text: str) -> int:
    return sum(1 for ch in text if ch in "（(") - sum(1 for ch in text if ch in "）)")


def extract_ready_segments(raw: str, cursor: int) -> tuple[list[str], int]:
    """从 `raw[cursor:]` 里切出**已经说完的整句**（D88）。返回 (句子列表, 新游标)。

    两个条件缺一不可：
      · **括号闭合**（depth == 0）——不然会切进"（她歪头看你一眼"里面，念出半句描写
      · 句末标点 + 可念字数 ≥ `MIN_SEGMENT_CHARS`——太碎的短句不值得单独合成一次
    """
    segments: list[str] = []
    depth = _paren_depth(raw[:cursor])
    start = cursor
    for index in range(cursor, len(raw)):
        char = raw[index]
        if char in "（(":
            depth += 1
        elif char in "）)":
            depth = max(0, depth - 1)
        elif depth == 0 and char in _SENTENCE_END:
            piece = raw[start : index + 1]
            if len(speakable_text(piece)) >= MIN_SEGMENT_CHARS:
                segments.append(piece)
                start = index + 1
    return segments, start


def extract_ready_paragraphs(raw: str, cursor: int) -> tuple[list[str], int]:
    """从 `raw[cursor:]` 里切出**已经说完的段**（D105）。返回 (段列表, 新游标)。

    两个切点（都在括号闭合的前提下）：
      · **空行**（自然段结束）
      · 一段的可念字数已经 ≥ `MAX_SEGMENT_CHARS`，又碰上句末标点 → 就在这儿切开

    D107：**只由括号组成的行不单独成段** —— 它是动作 / 心理描写，得挂在后面的台词上，
    合起来才算"一个自然段"（用户的原话："一个括号句一个真正句，这两个合起来才叫一个自然段"）。
    """
    segments: list[str] = []
    depth = _paren_depth(raw[:cursor])
    start = cursor
    index = cursor
    while index < len(raw):
        char = raw[index]
        if char in "（(":
            depth += 1
        elif char in "）)":
            depth = max(0, depth - 1)
        elif depth == 0:
            if char == "\n":
                match = _PARA_BREAK.match(raw, index)
                if match:
                    piece = raw[start:index]
                    # 只有括号（没有要念的字）→ 不切，等后面的台词
                    if speakable_text(piece):
                        segments.append(piece)
                        start = match.end()
                        index = start
                        continue
                    index = match.end()
                    continue
            elif char in _SENTENCE_END:
                piece = raw[start : index + 1]
                if len(speakable_text(piece)) >= MAX_SEGMENT_CHARS:
                    segments.append(piece)
                    start = index + 1
        index += 1
    # 游标别停在空白上（下一段不该以换行开头）
    while start < len(raw) and raw[start] in " \t\n":
        start += 1
    return segments, start


# ---------- 文件 ----------


def audio_path_of(message_id: str) -> Path:
    return config.AUDIO_DIR / f"{message_id}.wav"


def segment_dir_of(message_id: str) -> Path:
    """分段语音的目录：`user_data/audio/<message_id>/`（D105）。"""
    return config.AUDIO_DIR / message_id


def segment_path_of(message_id: str, index: int) -> Path:
    return segment_dir_of(message_id) / f"{index}.wav"


def segment_rel_path(message_id: str, index: int) -> str:
    return f"audio/{message_id}/{index}.wav"


def segment_url(message: Message | None, index: int) -> str | None:
    if message is None:
        return None
    return f"/api/friends/{message.friend_id}/messages/{message.id}/audio/{index}"


def audio_rel_path(message_id: str) -> str:
    return f"audio/{message_id}.wav"


def resolve_audio_path(message: Message) -> Path | None:
    """把 `messages.audio_path` 解析成真实文件（**挡住路径穿越**）；文件不在就 None。"""
    if not message.audio_path:
        return None
    base = config.USER_DATA_DIR.resolve()
    path = (config.USER_DATA_DIR / message.audio_path).resolve()
    if path != base and base not in path.parents:
        return None
    return path if path.is_file() else None


def has_audio(message: Message) -> bool:
    """这条消息现在真的能播吗（状态是 ready **且**文件还在）。"""
    return message.audio_status == READY and resolve_audio_path(message) is not None


def segments_of(message: Message) -> list[dict]:
    """这条消息的分段清单（D105）；没有 / 坏了都当空。"""
    if not message.audio_segments:
        return []
    try:
        parsed = json.loads(message.audio_segments)
    except json.JSONDecodeError:
        return []
    return parsed if isinstance(parsed, list) else []


def resolve_segment_path(message: Message, index: int) -> Path | None:
    """按消息里记的路径解析分段音频（**不信前端传的下标**，防路径穿越）。"""
    # v3.11（D178）：这段**正在流式合成**时路径可以现算 —— 那会儿 `audio_segments` 里
    # 还没写 `path`、文件也可能刚建出来；端点会用它"尾随"这个还在长大的文件。
    streaming = is_streaming(message.id, index)
    for item in segments_of(message):
        if int(item.get("index", -1)) != index:
            continue
        rel = item.get("path")
        if not rel:
            return segment_path_of(message.id, index) if streaming else None
        base = config.USER_DATA_DIR.resolve()
        path = (config.USER_DATA_DIR / str(rel)).resolve()
        if path != base and base not in path.parents:
            return None
        if path.is_file():
            return path
        return segment_path_of(message.id, index) if streaming else None
    # 连 meta 都还没有（流式刚起步就来取）→ 只有流式中才给出路径
    return segment_path_of(message.id, index) if streaming else None


def segments_public(message: Message) -> list[dict]:
    """给前端的形状（api.md 的【v2.2】2）。"""
    out: list[dict] = []
    for item in segments_of(message):
        index = int(item.get("index", -1))
        path = resolve_segment_path(message, index)
        if path is None:
            continue
        out.append(
            {
                "index": index,
                "url": segment_url(message, index),
                "duration_ms": item.get("duration_ms"),
                "text": str(item.get("text") or ""),
                # D112：这一段的情绪（老消息没有就是 None）
                "mood": item.get("mood") or None,
            }
        )
    return out


def audio_url(message: Message) -> str | None:
    return (
        f"/api/friends/{message.friend_id}/messages/{message.id}/audio"
        if has_audio(message)
        else None
    )


def _snapshot(voice: Voice) -> SimpleNamespace:
    """把 ORM 对象里 tts_service 需要的字段抄出来——**别把 session 绑定的对象带进线程**。"""
    return SimpleNamespace(
        name=voice.name,
        gpt_weights_path=voice.gpt_weights_path,
        sovits_weights_path=voice.sovits_weights_path,
        ref_audio_path=voice.ref_audio_path,
        prompt_text=voice.prompt_text,
        speed=voice.speed,
    )


def _duration_ms(raw: bytes) -> int | None:
    """从 wav 头部算时长（不引第三方库；算不出来就算了，前端不画进度条也能用）。"""
    try:
        with wave.open(io.BytesIO(raw), "rb") as wav:
            frames = wav.getnframes()
            rate = wav.getframerate()
    except (wave.Error, EOFError, OSError):
        return None
    if not rate:
        return None
    return int(frames * 1000 / rate)


def _concat_wavs(chunks: list[bytes]) -> tuple[bytes | None, str | None]:
    """把几段 wav 拼成一条（D88）。格式不一致就返回错误，让调用方给人话。"""
    if not chunks:
        return None, "没有可拼的音频"
    if len(chunks) == 1:
        return chunks[0], None

    readers: list[wave.Wave_read] = []
    try:
        for chunk in chunks:
            readers.append(wave.open(io.BytesIO(chunk), "rb"))
        first = readers[0]
        params = (
            first.getnchannels(),
            first.getsampwidth(),
            first.getframerate(),
            first.getcomptype(),
        )
        for reader in readers:
            current = (
                reader.getnchannels(),
                reader.getsampwidth(),
                reader.getframerate(),
                reader.getcomptype(),
            )
            if current != params:
                return None, "分段音频格式不一致——点「重新合成」会用整段重来一次"
        if params[3] != "NONE":
            return None, "分段音频是压缩格式，拼不了"

        out = io.BytesIO()
        with wave.open(out, "wb") as writer:
            writer.setnchannels(params[0])
            writer.setsampwidth(params[1])
            writer.setframerate(params[2])
            writer.setcomptype("NONE", "not compressed")
            for reader in readers:
                writer.writeframes(reader.readframes(reader.getnframes()))
        return out.getvalue(), None
    except (wave.Error, EOFError, OSError) as exc:
        logger.warning("拼接语音片段失败：{}", exc)
        return None, "音频片段读不出来"
    finally:
        for reader in readers:
            try:
                reader.close()
            except Exception:  # noqa: BLE001
                pass


# ---------- 状态 / 推送 ----------


def _mark(
    message: Message,
    status: str | None,
    *,
    path: str | None,
    duration: int | None,
    error: str | None,
) -> None:
    # 2026-10-02（用户实测）：**已经 READY、文件还在的那条，不许被"迟到的失败"覆盖**。
    # 现象："音频出来了、播了一会儿 → 突然写'这次没说出来'，可文件其实在"。
    # 只有真的没有文件（或本来就不是 READY）才允许标 failed。
    if status == FAILED and message.audio_status == READY and message.audio_path:
        return
    message.audio_status = status
    message.audio_path = path
    message.audio_duration_ms = duration
    message.audio_error = error


def _event(
    message_id: str,
    friend_id: str,
    status: str | None,
    *,
    duration: int | None = None,
    error: str | None = None,
) -> dict:
    """WS 推送用的载荷（api.md 的【v2】5）。只更新那条语音条，不动整页。"""
    return {
        "type": "audio_status",
        "friend_id": friend_id,
        "message_id": message_id,
        "audio_status": status,
        "audio_url": (
            f"/api/friends/{friend_id}/messages/{message_id}/audio" if status == READY else None
        ),
        "audio_duration_ms": duration,
        "audio_error": error,
    }


async def _set_status(
    message_id: str,
    friend_id: str,
    status: str | None,
    *,
    path: str | None = None,
    duration: int | None = None,
    error: str | None = None,
) -> None:
    """写库 + 推 WS（一条消息的语音状态变化都走这里）。"""
    with session_scope() as session:
        message = session.get(Message, message_id)
        if message is not None:
            _mark(message, status, path=path, duration=duration, error=error)
    await manager.broadcast(_event(message_id, friend_id, status, duration=duration, error=error))


def _segment_event(
    message_id: str,
    friend_id: str,
    index: int,
    duration: int | None,
    text: str,
    mood: str | None = None,
    *,
    streaming: bool = False,
    failed: bool = False,
) -> dict:
    """D105：**某一段**的语音好了就推一条 —— 前端据此"一段一段说"。"""
    event = {
        "type": "audio_segment",
        "friend_id": friend_id,
        "message_id": message_id,
        "index": index,
        "audio_url": f"/api/friends/{friend_id}/messages/{message_id}/audio/{index}",
        "audio_duration_ms": duration,
        "text": text,
        # D112：这一段的情绪（她每段末尾都给了标记）—— 前端按段换立绘
        "mood": mood,
    }
    # v3.11（D178）：`streaming=True` = "这段**开始**合成了，现在就能开始播（边生成边播）"
    if streaming:
        event["streaming"] = True
    if failed:
        event["failed"] = True
    return event


def follow_segment(path, message_id: str, index: int, *, poll: float = 0.05, idle_seconds: float = 120.0):  # noqa: ANN001
    """尾随"**正在写**"的分段文件：从头发到它写完（v3.11 / D178）。

    给 `StreamingResponse` 用（同步生成器，Starlette 会丢到线程池）：
    前端一个 `<audio>` 连上来，首字节 ~2 秒就能听到她开口 —— 不用等整段合成完。
    写完了（或失败/没人再写）就收工；中间允许短暂没数据（上游还在算）。
    """
    import time as _time

    key = _stream_key(message_id, index)
    sent = 0
    idle = 0.0
    while True:
        try:
            data = path.read_bytes()
        except OSError:
            data = b""
        if len(data) > sent:
            yield data[sent:]
            sent = len(data)
            idle = 0.0
        elif key not in _streaming:
            return  # 写完了，而且没有新数据 → 收工
        if key not in _streaming:
            return
        _time.sleep(poll)
        idle += poll
        if idle > idle_seconds:
            logger.warning("尾随分段音频超时（{} 秒没新数据）", idle_seconds)
            return


def _save_segments(message_id: str, meta: list[dict]) -> None:
    """把分段清单写进 `messages.audio_segments`（D105）。"""
    with session_scope() as session:
        message = session.get(Message, message_id)
        if message is None:
            return
        message.audio_segments = json.dumps(meta, ensure_ascii=False) if meta else None


def _clear_segments(message_id: str) -> None:
    """删掉这条消息的分段目录（重新合成 / 整段合成时先清干净）。"""
    shutil.rmtree(segment_dir_of(message_id), ignore_errors=True)


async def _store_ready(message_id: str, friend_id: str, raw: bytes) -> None:
    """落盘 + 标 ready（两条路共用）。"""
    duration = _duration_ms(raw)
    path = audio_path_of(message_id)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(raw)
    except OSError as exc:
        logger.warning("语音写盘失败（{}）：{}", path, exc)
        await _set_status(message_id, friend_id, FAILED, error="音频存不下来（磁盘？）")
        return

    with session_scope() as session:
        message = session.get(Message, message_id)
        if message is None:  # 中途被删了 → 刚写的文件也不留（删除要彻底）
            path.unlink(missing_ok=True)
            return
        _mark(message, READY, path=audio_rel_path(message_id), duration=duration, error=None)

    await manager.broadcast(_event(message_id, friend_id, READY, duration=duration))
    logger.info("语音已配到消息上（{}…，{} ms）", message_id[:8], duration)


async def _synth_bytes(
    text: str, voice: SimpleNamespace, base_url: str, sample_steps: int
) -> tuple[bytes | None, str | None]:
    """合成一段（线程里跑，不占事件循环）；失败返回 (None, 人话)。"""
    try:
        if os.environ.get("YF_MOBILE") == "1":
            from backend import mobile_voice  # noqa: PLC0415

            raw = await asyncio.to_thread(mobile_voice.synthesize, text)
            return raw, None
        raw = await asyncio.to_thread(
            tts_service.synthesize,
            text=text,
            voice=voice,
            base_url=base_url,
            sample_steps=sample_steps,
        )
        return raw, None
    except ServiceError as exc:
        return None, exc.message
    except Exception as exc:  # noqa: BLE001
        logger.warning("配音合成异常：{}", exc)
        return None, f"合成时出了点问题（{type(exc).__name__}）"


# ---------- 路 A：整段合成 ----------


def schedule(message_id: str, *, force: bool = False) -> bool:
    """给一条助手消息**整段**排队配音。**必须在事件循环里调**（要 `create_task`）。

    返回"排上了没有"：
      · 没开开关 / 没配音色 / 没正文 → False（什么都不做）
      · 已经有音频且文件还在 → False（除非 `force`）
      · 已经在合成 → False（不重复排队）
    """
    running = _running.get(message_id)
    if running is not None and not running.done():
        return False
    loop = _active_loop()
    if loop is None:  # 没有事件循环（脚本 / 同步单测直接调）→ 不排
        return False

    with session_scope() as session:
        message = session.get(Message, message_id)
        if message is None or message.role != "assistant":
            return False
        if not speakable_text(message.content):
            return False
        if not force:
            friend = session.get(Friend, message.friend_id)
            mobile = os.environ.get("YF_MOBILE") == "1"
            if (
                friend is None
                or not friend.voice_reply_enabled
                or (not mobile and not friend.voice_id)
            ):
                return False
            if has_audio(message):
                return False

    def _start() -> None:
        task = loop.create_task(_run(message_id, force=force))
        _running[message_id] = task
        task.add_done_callback(lambda _t, mid=message_id: _running.pop(mid, None))

    try:
        asyncio.get_running_loop()
    except RuntimeError:  # 同步端点（线程池）→ 丢回主循环
        loop.call_soon_threadsafe(_start)
    else:
        _start()
    return True


async def _run(message_id: str, *, force: bool) -> None:
    try:
        await _run_inner(message_id, force=force)
    except Exception as exc:  # noqa: BLE001 —— 配音失败绝不许影响别的任何事
        logger.warning("给消息配音失败（{}）：{}", message_id, exc)


async def _run_inner(message_id: str, *, force: bool) -> None:
    friend_id = ""
    prepared: dict | None = None
    immediate_fail: str | None = None

    with session_scope() as session:
        message = session.get(Message, message_id)
        if message is None or message.role != "assistant":
            return
        friend_id = message.friend_id
        friend = session.get(Friend, friend_id)
        voice = session.get(Voice, friend.voice_id) if (friend and friend.voice_id) else None
        mobile = os.environ.get("YF_MOBILE") == "1"
        if voice is None and mobile and friend is not None and friend.voice_reply_enabled:
            # 手机端没有桌面音色文件；用系统/云端引擎，不要求 voices 表。
            voice = SimpleNamespace(
                id="mobile",
                name="手机语音",
                gpt_weights_path="",
                sovits_weights_path="",
                ref_audio_path="",
                prompt_text="",
                speed=1.0,
            )
        text = speakable_text(message.content)
        if voice is None or not text:
            immediate_fail = (
                "这个朋友还没配音色。"
                if voice is None
                else "这条只有动作或心里的想法，没有她要说的话"
            )
            _mark(message, FAILED, path=None, duration=None, error=immediate_fail)
        elif not force and has_audio(message):
            return
        else:
            purposes = settings_service.effective_purposes_from(settings_service.get(session))
            tts_config = tts_service.tts_settings(purposes)
            # 整段合成：把上次留下的分段清干净（D105）——这条以后只有一个 wav
            message.audio_segments = None
            _mark(message, PENDING, path=None, duration=None, error=None)
            prepared = {
                "voice": _snapshot(voice),
                "text": text,
                "base_url": tts_config["base_url"],
                "sample_steps": tts_config["sample_steps"],
            }

    if immediate_fail is not None:
        await manager.broadcast(_event(message_id, friend_id, FAILED, error=immediate_fail))
        return

    _clear_segments(message_id)
    await manager.broadcast(_event(message_id, friend_id, PENDING))
    raw, error = await _synth_bytes(
        prepared["text"], prepared["voice"], prepared["base_url"], prepared["sample_steps"]
    )
    if raw is None:
        await _set_status(message_id, friend_id, FAILED, error=error or "合成失败")
        return
    await _store_ready(message_id, friend_id, raw)
    # 手机端：再写一份第 0 段，CallView 走 audio_segment 事件。
    if os.environ.get("YF_MOBILE") == "1":
        try:
            path = segment_path_of(message_id, 0)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(raw)
            duration = _duration_ms(raw)
            _save_segments(
                message_id,
                [{"index": 0, "path": segment_rel_path(message_id, 0), "duration_ms": duration, "text": text}],
            )
            await manager.broadcast(
                _segment_event(message_id, friend_id, 0, duration, text)
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("手机分段语音写入失败（不影响整段语音）：{}", exc)


# ---------- 路 B：边流边合成（D88 Level 1）----------


class StreamingVoice:
    """一条回复的"边流边合成"（D88）。

    用法：`open_stream()` 拿到实例 → 每段 delta 调 `feed()` → 结束时 `finish(全文)`。
    `finish()` 是**协程**，调用方用 `launch_finish()` 丢进后台做，别堵着 SSE 响应。
    """

    def __init__(
        self,
        *,
        message_id: str,
        friend_id: str,
        voice: SimpleNamespace,
        base_url: str,
        sample_steps: int,
    ) -> None:
        self.message_id = message_id
        self.friend_id = friend_id
        self._voice = voice
        self._base_url = base_url
        self._sample_steps = sample_steps
        self._raw = ""
        self._cursor = 0
        self._tasks: list[asyncio.Task] = []  # type: ignore[type-arg]
        self._results: dict[int, bytes] = {}
        self._meta: dict[int, dict] = {}
        self._mood: str | None = None  # D112：最近一次摘到的情绪（贴在接下来的段上）
        self._count = 0
        self._failed: str | None = None
        # v3.2（D148）：每合成完一段就往这个队列丢个信号 —— SSE 那边据此"放文字"。
        # `finish()` 收工时丢一个 `None` 当哨兵（成功 / 失败 / 超时都必须丢，不能让人干等）。
        self._notify: asyncio.Queue | None = None
        self.settled = False

    def attach(self, queue: asyncio.Queue) -> None:
        """把"这一段好了"的通知接出来（D148；由 `sse_stream` 调）。"""
        self._notify = queue

    def _notify_segment(self) -> None:
        if self._notify is not None:
            self._notify.put_nowait(self.message_id)

    # -- 喂文字 --

    def feed(self, delta: str) -> None:
        """流式文字进来一段（**不会抛**：出错就整条退回失败，文字不受影响）。"""
        if self._failed or not delta:
            return
        self._raw += delta
        try:
            pieces, self._cursor = extract_ready_paragraphs(self._raw, self._cursor)
        except Exception as exc:  # noqa: BLE001 —— 切段失败不该影响聊天
            logger.warning("切段失败，这条改成整段合成：{}", exc)
            self._failed = "切段时出了点问题"
            return
        for piece in pieces:
            self._dispatch(piece)

    def mark_mood(self, mood: str | None) -> None:
        """D112：这一轮又摘到一个表情标记 —— 之后切出来的段都带它。"""
        if mood:
            self._mood = mood

    def _dispatch(self, piece: str) -> None:
        """把一段排进合成队列（段=自然段；段内一次合成，D105）。"""
        text = speakable_text(piece)
        if not text:
            return
        index = self._count
        self._count += 1
        self._meta[index] = {"text": piece.strip(), "mood": self._mood}
        task = asyncio.get_running_loop().create_task(self._synth(index, text))
        self._tasks.append(task)

    async def _synth(self, index: int, text: str) -> None:
        logger.info("分段配音开始 第 {} 段（{} 字）", index + 1, len(text))
        meta = self._meta[index]
        path = segment_path_of(self.message_id, index)
        key = _stream_key(self.message_id, index)
        # v3.11（D178）：**先推"这段开始合成了"** —— 前端拿到 URL 就能边生成边播。
        # 实测：整段 6 秒，首字节 ~2 秒 → 这 4 秒就是耳朵能省下来的。
        _streaming.add(key)
        await manager.broadcast(
            _segment_event(
                self.message_id,
                self.friend_id,
                index,
                None,
                meta["text"],
                meta.get("mood"),
                streaming=True,
            )
        )
        raw: bytes | None = None
        try:
            written = await asyncio.to_thread(
                tts_service.synthesize_stream_to,
                text=text,
                voice=self._voice,
                base_url=self._base_url,
                sample_steps=self._sample_steps,
                path=path,
            )
        except Exception as exc:  # noqa: BLE001 —— 流式这条路自己兜底
            logger.warning("第 {} 段流式合成异常：{}", index + 1, exc)
            written = 0
        if written:
            # 占位头 → 真实长度（不修的话 wave 模块读不了、拼接也会坏）
            if tts_service.fix_wav_header(path):
                try:
                    raw = path.read_bytes()
                except OSError:
                    raw = None
            else:
                logger.warning("第 {} 段流式文件不完整（{} 字节），丢掉", index + 1, written)
            if raw is None:
                # 文件写好了但读不回来（磁盘？）→ 按这段失败处理，别把 None 传下去
                _streaming.discard(key)
                if self._failed is None:
                    self._failed = "这段音频没读回来"
                await manager.broadcast(
                    _segment_event(
                        self.message_id, self.friend_id, index, None, meta["text"],
                        meta.get("mood"), failed=True,
                    )
                )
                return
        else:
            # 上游不支持流式 / 连不上 / 中途断了 → 退回"整段合成"那条老路（别让这段没了）
            logger.info("第 {} 段流式没成，退回整段合成", index + 1)
            fallback, error = await _synth_bytes(
                text, self._voice, self._base_url, self._sample_steps
            )
            if fallback is not None:
                try:
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_bytes(fallback)
                    raw = fallback
                except OSError as exc:
                    # 分段文件写不下来不算整条失败：收尾照样会把整条 wav 存好
                    logger.warning("分段语音写盘失败（{}）：{}", path, exc)
            if raw is None:
                _streaming.discard(key)
                logger.warning("分段配音失败 第 {} 段：{}", index + 1, error)
                if self._failed is None:
                    self._failed = error or "这段没合成出来"
                await manager.broadcast(
                    _segment_event(
                        self.message_id,
                        self.friend_id,
                        index,
                        None,
                        meta["text"],
                        meta.get("mood"),
                        failed=True,
                    )
                )
                return
        _streaming.discard(key)
        self._results[index] = raw
        duration = _duration_ms(raw)
        logger.info("分段配音好了 第 {} 段（{} ms）", index + 1, duration or 0)
        self._meta[index]["path"] = segment_rel_path(self.message_id, index)
        self._meta[index]["duration_ms"] = duration
        await manager.broadcast(
            _segment_event(
                self.message_id,
                self.friend_id,
                index,
                duration,
                self._meta[index]["text"],
                self._meta[index].get("mood"),
            )
        )
        # v3.2（D148）：这一段真能播了 → 通知 SSE 把对应的文字放出去
        self._notify_segment()

    # -- 收尾 --

    def abort(self) -> None:
        """中途被打断（用户按停止 / 生成出错）→ 取消片段任务，不产出音频。"""
        for task in self._tasks:
            task.cancel()
        self._tasks.clear()
        # 别让等队列的 SSE 协程干等（D148）
        self.settled = True
        if self._notify is not None:
            self._notify.put_nowait(None)

    async def finish(self, full_text: str) -> None:
        try:
            await self._finish_inner(full_text)
        finally:
            # D148：收工哨兵 —— 成功 / 失败 / 超时都要丢，否则文字可能迟迟不放
            self.settled = True
            if self._notify is not None:
                self._notify.put_nowait(None)

    async def _finish_inner(self, full_text: str) -> None:
        """文字流完了：补最后一段 → 等分段 → 拼成整条 → 标 ready / failed。"""
        # 最后那一截（可能没有空行收尾）也要念
        tail = self._raw[self._cursor :]
        if full_text.startswith(self._raw):
            # 极少见：SSE 累积的文本和落库正文不一致（工具块剥离等）→ 用落库正文兜底
            tail = full_text[self._cursor :]
        self._dispatch(tail)

        if not self._tasks and not self._results:
            # 整条只有动作 / 心理描写 → 本来就没话可念：清干净，前端回到「念给我听」
            _clear_segments(self.message_id)
            await _set_status(self.message_id, self.friend_id, None)
            return

        # 还在合成 → 先标 pending（前端显示"正在合成…"）；已经全好了也标一下，
        # 让前端拿到"pending → ready"这个顺序（自动播放的判据之一）
        await _set_status(self.message_id, self.friend_id, PENDING)
        # D107：总时长上限 —— 到点就"能听多少听多少"，把还没开始的段取消掉
        timed_out = False
        if self._tasks:
            done, pending = await asyncio.wait(
                self._tasks, timeout=VOICE_TOTAL_TIMEOUT_SECONDS
            )
            if pending:
                timed_out = True
                logger.warning(
                    "配音总时长超过 {:.0f} 秒，放弃剩下 {} 段（已好的照常给用户）",
                    VOICE_TOTAL_TIMEOUT_SECONDS,
                    len(pending),
                )
                for task in pending:
                    task.cancel()

        if self._failed is not None and not self._results:
            _clear_segments(self.message_id)
            await _set_status(self.message_id, self.friend_id, FAILED, error=self._failed)
            return
        if self._failed is not None and self._results:
            # 一部分好了、一部分坏了：把好的给用户，状态仍算 ready（别整条作废）
            logger.warning("配音部分失败（{}），先把已经好的段给用户", self._failed)
        if timed_out:
            logger.warning("配音超时收尾（已拿到 {} 段）", len(self._results))

        ordered = [self._results[index] for index in sorted(self._results)]
        if not ordered:
            _clear_segments(self.message_id)
            await _set_status(self.message_id, self.friend_id, None)
            return

        combined, error = _concat_wavs(ordered)
        if combined is None:
            _clear_segments(self.message_id)
            await _set_status(
                self.message_id, self.friend_id, FAILED, error=error or "音频拼接失败"
            )
            return
        await _store_ready(self.message_id, self.friend_id, combined)
        if timed_out:
            # v3.11（D178）：超时不再只是日志里的一句话 —— 界面上给一句能照做的提示
            # （已经拿到的段照常能听；这句只是解释"为什么后面没声"）
            with session_scope() as session:
                row = session.get(Message, self.message_id)
                if row is not None:
                    row.audio_error = (
                        "后面几段没来得及合成（语音服务太慢/显卡被占着）—— "
                        "点「重新合成」可以再试；跑语音时关掉游戏、壁纸引擎会快很多"
                    )
                    session.flush()

        # D105：记下分段清单（≥2 段才有意义；只有一段就清掉，前端用整条那个 wav）
        meta = [
            {"index": index, **self._meta[index]}
            for index in sorted(self._meta)
            if index in self._results and self._meta[index].get("path")
        ]
        if len(meta) >= 2:
            _save_segments(self.message_id, meta)
        else:
            _clear_segments(self.message_id)
            _save_segments(self.message_id, [])


def open_stream(message_id: str) -> StreamingVoice | None:
    """回复**开始流**的时候调一次（D88）。

    返回 `None` = 这条不需要配音（没开开关 / 没配音色 / 找不到人）→ 调用方走整段合成。
    只读一次库（拿音色和 TTS 设置），后面全程在内存里干活。
    """
    if os.environ.get("YF_MOBILE") == "1":
        return None
    with session_scope() as session:
        message = session.get(Message, message_id)
        if message is None or message.role != "assistant":
            return None
        friend = session.get(Friend, message.friend_id)
        if friend is None or not friend.voice_reply_enabled or not friend.voice_id:
            return None
        voice = session.get(Voice, friend.voice_id)
        if voice is None:
            return None
        purposes = settings_service.effective_purposes_from(settings_service.get(session))
        tts_config = tts_service.tts_settings(purposes)
        return StreamingVoice(
            message_id=message_id,
            friend_id=friend.id,
            voice=_snapshot(voice),
            base_url=tts_config["base_url"],
            sample_steps=tts_config["sample_steps"],
        )


def launch_finish(builder: StreamingVoice, full_text: str) -> bool:
    """把 `finish()` 丢进后台（在事件循环里调）——**不阻塞 SSE 响应**。"""
    loop = _active_loop()
    if loop is None:
        return False
    try:
        running = asyncio.get_running_loop()
    except RuntimeError:
        running = None

    def _start() -> None:
        task = loop.create_task(builder.finish(full_text))
        _running[builder.message_id] = task
        task.add_done_callback(lambda _t, mid=builder.message_id: _running.pop(mid, None))

    if running is not None:
        _start()
    else:
        loop.call_soon_threadsafe(_start)
    return True


def reset_stale_pending() -> int:
    """启动时把上次没做完的 `pending` 标成 `failed`（进程重启后没人会再做完它）。"""
    with session_scope() as session:
        rows = list(
            session.execute(select(Message).where(Message.audio_status == PENDING)).scalars()
        )
        for message in rows:
            _mark(
                message,
                FAILED,
                path=message.audio_path,
                duration=message.audio_duration_ms,
                error="应用重启了，这条没合成完——重新合成一下就行",
            )
        return len(rows)


# ---------- 语音文件清理（v3.0 / D141）----------

# 消息语音留多久（0 = 永久留，只按总量清）
AUDIO_KEEP_DAYS = 30
# **最近多少条**有语音的消息永远留着 —— 免得你翻回昨天/前天的记录点回放，
# 满屏都是「重新合成」
AUDIO_KEEP_RECENT = 200
# 总量上限兜底（超了从最旧的开始删，保护名单里的不动）
AUDIO_MAX_BYTES = 1024 * 1024 * 1024


def remove_for_friend(session, friend_id: str) -> int:  # noqa: ANN001 —— Session（避免循环 import）
    """删掉某个朋友的**全部**语音文件（`<mid>.wav` + 分段目录）。给他"彻底删除"用。

    返回删掉的文件数。**先删文件再删消息行**（行没了就不知道要删哪些文件了）。
    """
    ids = list(
        session.execute(select(Message.id).where(Message.friend_id == friend_id)).scalars()
    )
    removed = 0
    for message_id in ids:
        removed += _remove_message_audio(str(message_id))
    return removed


def _remove_message_audio(message_id: str) -> int:
    """删一条消息的语音（单个文件 + 分段目录），返回删掉的条目数。"""
    removed = 0
    single = audio_path_of(message_id)
    if single.is_file():
        try:
            single.unlink()
            removed += 1
        except OSError as exc:  # noqa: PERF203 —— 删不掉也不能把删除流程搞挂
            logger.warning("语音文件删不掉（忽略）：{}", exc)
    segments = segment_dir_of(message_id)
    if segments.is_dir():
        try:
            shutil.rmtree(segments, ignore_errors=True)
            removed += 1
        except OSError as exc:
            logger.warning("分段语音目录删不掉（忽略）：{}", exc)
    return removed


def purge_audio(
    session,  # noqa: ANN001 —— Session
    *,
    keep_days: int = AUDIO_KEEP_DAYS,
    keep_recent: int = AUDIO_KEEP_RECENT,
    max_bytes: int = AUDIO_MAX_BYTES,
    now: float | None = None,
) -> dict[str, int]:
    """清消息语音，返回 `{"orphans", "expired", "oversize", "removed", "freed"}`。

    三种该删的：

    1. **孤儿**：文件在，但消息行已经没了（删过朋友 / 删过消息）→ 直接删（宪法§9 不许留 orphan）
    2. **过期**：超过 `keep_days` 没动过的 → 删（`keep_days=0` = 永久留）
    3. **超量**：清完还超过 `max_bytes` → 从最旧删起

    **最近 `keep_recent` 条有语音的消息永远不删**（1、3 两种也保护不了它们：孤儿才不管新旧，
    但"还在的消息"里最近那批一定留着）。
    """
    directory = config.AUDIO_DIR
    if not directory.is_dir():
        return {"orphans": 0, "expired": 0, "oversize": 0, "removed": 0, "freed": 0}
    if not config.cleanup_targets_are_consistent():
        # 库和文件目录不是同一份 user_data → 谁是孤儿就说不准了，宁可不删（2026-09-21 的事故闸）
        logger.warning("库和语音目录不在同一个 user_data 里，本轮**拒绝清理**（防止误删）")
        return {"orphans": 0, "expired": 0, "oversize": 0, "removed": 0, "freed": 0}
    current = now if now is not None else time.time()
    cutoff = current - keep_days * 86400 if keep_days > 0 else None

    # 哪些消息"还活着"、哪些是最近的要保护
    rows = session.execute(select(Message.id, Message.created_at)).all()
    alive = {str(row[0]) for row in rows}
    recent_order = [
        str(row[0])
        for row in session.execute(
            select(Message.id)
            .where(Message.audio_status == READY)
            .order_by(Message.created_at.desc())
            .limit(keep_recent)
        ).all()
    ]
    protected = set(recent_order)

    orphans = expired = oversize = 0
    freed = 0
    survivors = []  # [(path, mtime)] 留着等会儿判断总量

    entries = [item for item in directory.iterdir() if item.name != ".gitkeep"]
    for entry in sorted(entries):
        message_id = entry.name[:-4] if entry.is_file() and entry.suffix == ".wav" else entry.name
        size, mtime = _entry_size_mtime(entry)
        if message_id not in alive:
            _unlink_entry(entry)
            orphans += 1
            freed += size
            continue
        if message_id in protected:
            continue
        if cutoff is not None and mtime < cutoff:
            _unlink_entry(entry)
            expired += 1
            freed += size
            continue
        survivors.append((entry, mtime, size))

    total = sum(size for _entry, _mtime, size in survivors)
    if total > max_bytes:
        for entry, _mtime, size in sorted(survivors, key=lambda item: item[1]):
            if total <= max_bytes:
                break
            _unlink_entry(entry)
            oversize += 1
            freed += size
            total -= size

    removed = orphans + expired + oversize
    if removed:
        logger.info(
            "清理消息语音：{} 项 / {:.1f} MB（孤儿 {} / 过期 {} / 超量 {}）",
            removed,
            freed / 1024 / 1024,
            orphans,
            expired,
            oversize,
        )
    return {
        "orphans": orphans,
        "expired": expired,
        "oversize": oversize,
        "removed": removed,
        "freed": freed,
    }


def _entry_size_mtime(entry: Path) -> tuple[int, float]:
    """单个文件 / 分段目录的总大小和"最后一次改动时间"。"""
    if entry.is_file():
        try:
            stat = entry.stat()
            return stat.st_size, stat.st_mtime
        except OSError:
            return 0, 0.0
    size = 0
    newest = 0.0
    for child in entry.rglob("*"):
        try:
            stat = child.stat()
        except OSError:
            continue
        if child.is_file():
            size += stat.st_size
        newest = max(newest, stat.st_mtime)
    return size, newest


def _unlink_entry(entry: Path) -> None:
    try:
        if entry.is_dir():
            shutil.rmtree(entry, ignore_errors=True)
        else:
            entry.unlink()
    except OSError as exc:  # noqa: PERF203
        logger.warning("语音文件删不掉（忽略）：{}", exc)
