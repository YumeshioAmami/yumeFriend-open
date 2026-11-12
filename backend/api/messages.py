"""聊天路由：发消息 = SSE 流（api.md §2.1，唯一入口）。

时序（architecture.md §3）::

    ① 落库 user 消息
    ② 组装上下文（agent/context）
    ③ 流式调用 LLM（agent/loop）+ 工具块剥离（agent/parser）
    ④ 落库 assistant 消息（干净正文 + stream_status + tools_used）
    ⑤ 发 done 事件

客户端断开 → 已生成的部分照样落库，标 `stream_status = partial`（api.md §1.6 / D30）。
"""

from __future__ import annotations

import asyncio
import json
import re
import time
from datetime import datetime, timezone
from typing import Any, AsyncIterator, Callable, Sequence

from fastapi import APIRouter, Depends, Query
from fastapi.responses import FileResponse, StreamingResponse
from loguru import logger
from sqlalchemy.orm import Session
from sqlalchemy import update

from backend.agent import loop, reply_runs
from backend.agent import pacing
from backend.agent import bubble_splitter
from backend.agent.context import HISTORY_LIMIT_DEFAULT
from backend.agent.state_block import strip_state_blocks
from backend.api.ws import manager
from backend.api.deps import get_db, get_provider_factory
from backend.db import session_scope
from backend.features import events as feature_events
from backend.features import flags as feature_flags
from backend.llm.base import ILLMProvider
from backend.llm.errors import LLMCallError
from backend.models import Message
from backend.schemas.message import MessageCreate
from backend.services import (
    character_state_service,
    episode_service,
    expression_service,
    friend_service,
    portrait_service,
    screen_service,
    image_service,
    image_note_service,
    knowledge_service,
    life_service,
    live2d_settings_service,
    memory_service,
    message_service,
    portrait_pack_service,
    settings_service,
    space_service,
    sync_service,
    tts_service,
    typo_service,
    turn_scheduler,
    vision_service,
    voice_reply_service,
)
from backend.services.actor import Actor
from backend.services.errors import NotFoundError, ServiceError

router = APIRouter(prefix="/api/friends", tags=["messages"])

SSE_HEADERS = {
    "Cache-Control": "no-cache",
    "Connection": "keep-alive",
    "X-Accel-Buffering": "no",  # 别让中间层缓冲，否则打字机变"一次蹦出来"
}

# v3.2（D148）：文字"等她的语音"最多等这么久 —— 到点了就先把文字补齐，
# **绝不**让聊天因为语音卡住而干等（语音晚到了照样会自动播）。
# 正常情况用不到它：合成失败 / 超时都会给"收工"信号，这条只是卡死的最后一道闸。
# 模块级常量是给测试留的旋钮。
# v3.12（D180）：用户要求"等语音合成完再出文字"，而合成 ≈ 1:1 实时（常有 1 分钟音频）→
# 30 秒太短（文字会比声音早太多）→ 抬到 120 秒（仍是安全闸，不是硬等）。
VOICE_RELEASE_TIMEOUT_SECONDS = 120.0

# v3.14（D182）：**边生成边把正文写回库** —— 关掉窗口再打开时能看到她说到哪了
# （不写的话库里那条一直是空的）。最多 1 秒写一次，别把 sqlite 写爆。
PROGRESS_FLUSH_SECONDS = 1.0


def _sse(event: str, data: dict) -> str:
    """SSE 帧（LLM协议§3.2 的事件表）。"""
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


async def _write_off_loop(fn: Callable[..., None], *args: Any, **kwargs: Any) -> bool:
    """把一次"落库"挪到线程里跑，并**在撞锁时退避重试**（2026-09-27 / D197）。

    为什么非改不可（实测证据）：
      · `_flush_progress` / `_finalize` 都是**同步** SQLite 写，原先**直接在 async 流里调用**；
      · 一旦撞上 `database is locked`，`busy_timeout` 那 **5 秒会把整个事件循环堵死** ——
        这期间后端什么请求都不接（手机端整片卡住），于是用户看到的是
        "**她一开始输出就断、重试也连不上**"（每次重试又撞上同样的 5 秒）；
      · 最后那次落库撞锁时，`reply_runs` 的兜底 catch 直接把**整轮回复干掉**
        （日志："后台回复任务崩了：database is locked"）→ 用户那边就是"消息没了"。

    现在：① 挪线程 —— 就算真撞锁，也只卡那一个线程，**事件循环照样转**（手机不再被殃及）；
          ② 退避重试 3 次 —— 偶发抢锁不至于丢一条回复。
    """
    for attempt in range(3):
        try:
            await asyncio.to_thread(fn, *args, **kwargs)
            return True
        except Exception as exc:  # noqa: BLE001 —— 落库失败不能带崩这一轮
            locked = "locked" in str(exc).lower()
            if not locked or attempt >= 2:
                logger.warning("落库失败（第 {} 次{}）：{}", attempt + 1, "，放弃" if attempt >= 2 else "", exc)
                return False
            await asyncio.sleep(0.5 * (2**attempt))
    return False


def _flush_progress(message_id: str, content: str, moods: list[str]) -> None:
    """把"已经生成的正文"落一次库（D182）。独立会话：后台 task 不该碰请求会话。

    ⚠️ 2026-09-27（D197）：**必须是"第一条语句就是 UPDATE"**，不许先 `session.get()`。

    为什么（实测踩到的坑）：`session.get()` 是读，SQLAlchemy 会把整段包进一个事务
    → 提交时要把**读事务升级成写事务**；这期间只要**别的连接提交过**（她那边每轮工具
    调用都会 commit），SQLite 立刻报 `database is locked`（`SQLITE_BUSY_SNAPSHOT`
    —— 这种错**等也没用**，`busy_timeout` 白设，所以退避重试 3 次照样全失败）。
    改成直接 UPDATE → 事务**从一开始就是写事务**，抢不到锁就正常排队等
    → 那条"注定失败"的路就没了。
    """
    values: dict[str, Any] = {
        "content": strip_state_blocks(content),
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    if moods:
        values["moods"] = json.dumps(moods, ensure_ascii=False)
    with session_scope() as session:
        result = session.execute(
            update(Message)
            .where(Message.id == message_id, Message.stream_status == message_service.STREAMING)
            .values(**values)
        )
        if result.rowcount:
            sync_service.mark_message_dirty(session, message_id)


def _finalize(
    message_id: str,
    content: str,
    status: str,
    tool_calls: list[dict],
    *,
    sources: list[dict] | None = None,
    mood: str | None = None,
    moods: list[str] | None = None,
    segments: list[str] | None = None,
) -> None:
    """回填 assistant 消息。

    用**独立会话**：请求级会话的生命周期跟着响应走，流还没结束就断开时靠不住。
    同样（D197）：**第一条语句就是 UPDATE**，别先读 —— 读后写会撞 `SQLITE_BUSY_SNAPSHOT`。
    """
    content = strip_state_blocks(content)
    clean_segments = _clean_segments(segments)
    if len(clean_segments) > 1:
        content = "\n".join(clean_segments)
    values: dict[str, Any] = {
        "content": content,
        "stream_status": status,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    if len(clean_segments) > 1:
        values["segments"] = json.dumps(clean_segments, ensure_ascii=False)
    if tool_calls:
        values["tools_used"] = json.dumps(tool_calls, ensure_ascii=False)
    if sources:
        # v2（T2.0-7）：这轮搜到的来源，前端渲染成气泡下面的小链接
        values["sources"] = json.dumps(sources, ensure_ascii=False)
    if mood:
        # v2.2（D102）：她这轮用什么表情（沉浸模式 / 桌宠用；正文里已经摘掉了这行）
        values["mood"] = mood
    if moods:
        # v2.2（D113）：**每段**的情绪（按段顺序）—— 沉浸模式读到哪段换哪张脸
        values["moods"] = json.dumps(moods, ensure_ascii=False)
    with session_scope() as session:
        result = session.execute(update(Message).where(Message.id == message_id).values(**values))
        if result.rowcount:
            sync_service.mark_message_dirty(session, message_id)

    # v2.2（D117）：她画出来的图挂在**她自己这条消息**上（前端直接显示在气泡里）。
    # 极少见（只有她真画图时），所以放在**单独一趟**里做，别拖累上面那条热路径。
    drawn = next(
        (
            (item.get("result") or {}).get("image_rel")
            for item in tool_calls
            if item.get("ok") and isinstance(item.get("result"), dict)
        ),
        None,
    )
    if drawn:
        with session_scope() as session:
            message = session.get(Message, message_id)
            if message is not None:
                message.image_path = str(drawn)
                message.image_no = message_service.next_image_no(session, message.friend_id)
                sync_service.mark_message_dirty(session, message_id)


def _clean_segments(segments: list[str] | None) -> list[str]:
    cleaned = [strip_state_blocks(str(item)).strip() for item in (segments or [])]
    return [item for item in cleaned if item]


def _store_text(segments: list[str] | None, chunks: list[str]) -> str:
    clean = _clean_segments(segments)
    return "\n".join(clean) if len(clean) > 1 else strip_state_blocks("".join(chunks))


def _paragraph_segments(text: str) -> list[str]:
    """模型没写 <<<MSG>>> 时的兜底：自然段本身就是气泡边界。

    2026-11-09 实测：模型一段一个情绪标签，但回复有 8 个自然段。
    旧逻辑只肯拆 2~3 段，8 段就被当成 1 个 segment，后面的情绪全被对齐成第一段。
    """
    parts = [part.strip() for part in re.split(r"\n\s*\n", text or "") if part.strip()]
    if len(parts) <= 1:
        return []
    return parts[:20]


def _align_moods(
    segments: list[str] | None,
    marks: list[dict[str, Any]],
    *,
    fallback: str | None = None,
) -> list[str]:
    """把流里的情绪标记按字符偏移对齐到最终气泡，保证历史刷新前后表情不变。"""
    clean = _clean_segments(segments)
    if not clean:
        return []
    valid = [
        {"mood": str(item.get("mood") or ""), "at": int(item.get("at") or 0)}
        for item in marks
        if item.get("mood")
    ]
    if not valid:
        return [fallback] * len(clean) if fallback else []
    # 最常见的情况：一段一个情绪标签。提示词要求的就是“一段一个”；这时
    # 直接按顺序对齐，既不受空行/缩进造成的字符偏移影响，也不会把后几句
    # 全落成同一个情绪（用户 2026-11-08 实测的 bug）。
    if len(valid) == len(clean):
        return [item["mood"] for item in valid]
    starts: list[int] = []
    offset = 0
    for index, segment in enumerate(clean):
        starts.append(offset)
        offset += len(segment) + (1 if index < len(clean) - 1 else 0)
    out: list[str] = []
    last = ""
    for start in starts:
        for mark in valid:
            if mark["at"] <= start + 1:
                last = mark["mood"]
        out.append(last or valid[0]["mood"])
    return out


def _collect_sources(tool_calls: list[dict], *, limit: int = 5) -> list[dict]:
    """把这一轮 `web_search` 的结果收成来源列表（按 URL 去重，重新编号）。

    ⚠️ 一轮里搜了**多次**时，编号会按"出现顺序"重排；模型回答里的 [n] 以它自己那次搜索的
    编号为准 —— 单次搜索（绝大多数情况）两边完全一致。
    """
    seen: set[str] = set()
    collected: list[dict] = []
    for call in tool_calls:
        if call.get("name") != "web_search" or not call.get("ok"):
            continue
        for item in (call.get("result") or {}).get("results") or []:
            url = item.get("url")
            if not url or url in seen:
                continue
            seen.add(url)
            collected.append(
                {"no": len(collected) + 1, "title": item.get("title") or url, "url": url}
            )
            if len(collected) >= limit:
                return collected
    return collected


async def _maybe_aclose(provider: ILLMProvider) -> None:
    closer = getattr(provider, "aclose", None)
    if closer is None:
        return
    try:
        await closer()
    except Exception:  # noqa: BLE001 —— 收尾失败不该改变本轮业务结果
        logger.warning("关闭 provider 连接失败（不影响本轮结果）")


async def sse_stream(
    *,
    friend: Any,
    user_input: str,
    provider: ILLMProvider,
    history: Sequence[Any],
    memories: Sequence[Any],
    user_message_id: str,
    assistant_id: str,
    deep_thinking: bool,
    session: Session,
    previous_mode: str | None = None,
    user_image: str | None = None,
    inline_images: bool = True,
    knowledge_hits: Sequence[dict] = (),
    document_index: Sequence[dict] = (),
    screen_note: str | None = None,
    portrait_emotions: Sequence[str] = (),
    character_state: Any | None = None,
    episode_note: str | None = None,
    life_note: str | None = None,
    space_note: str | None = None,
    pacing_note: str | None = None,
    reply_shape_note: str | None = None,
    max_tokens_cap: int | None = None,
    bubble_max_chars: int | None = None,
    bubble_max_bubbles: int = bubble_splitter.DEFAULT_MAX_BUBBLES,
    expression_note: str | None = None,
    typo_rate: float = 0.0,
) -> AsyncIterator[str]:
    """把 agent 事件流转成 SSE 帧，并在结束时回填 assistant 消息。

    单独抽成模块级函数是为了能直接单测"客户端断开"这条路：
    `agen.aclose()` 触发的正是真实断开时抛进来的 `GeneratorExit`。
    """
    chunks: list[str] = []
    segments: list[str] = [""]
    tool_calls: list[dict] = []
    usage: dict = {}
    stop_reason: str | None = None
    mood: str | None = None
    moods: list[str] = []
    mood_marks: list[dict[str, Any]] = []
    # D88：开了「她的回复都用语音」→ 走"边流边合成"（一句说完就先合成那句）
    voice_builder = None
    try:
        voice_builder = voice_reply_service.open_stream(assistant_id)
    except Exception as exc:  # noqa: BLE001 —— 开不了就退回整段合成
        logger.warning("开启边流合成失败（这条改走整段合成）：{}", exc)
    # v3.2（D148）：文字**等她那段语音**再放出来（普通聊天也这样）。
    # `held` = 攒着还没放的正文；`voice_queue` = 流式合成器"这段好了"的通知。
    held: list[str] = []
    voice_queue: asyncio.Queue | None = None
    if voice_builder is not None:
        voice_queue = asyncio.Queue()
        voice_builder.attach(voice_queue)
    # D182：正文进度落库的节流（最多 1 秒一次）
    last_flush = time.monotonic()
    # D110：**先把这一轮的两条消息 id 告诉前端** —— 分段语音（audio_segment 事件）
    # 只有靠它才能精确绑到"正在说的这一轮"，不然前端只能猜（实测会串到上一轮）。
    yield _sse(
        "start",
        {"user_message_id": user_message_id, "assistant_message_id": assistant_id},
    )
    try:
        async for event in loop.stream_reply(
            friend=friend,
            user_input=user_input,
            provider=provider,
            session=session,
            history=history,
            memories=memories,
            deep_thinking=deep_thinking,
            previous_mode=previous_mode,
            user_image=user_image,
            inline_images=inline_images,
            knowledge_hits=knowledge_hits,
            document_index=document_index,
            screen_note=screen_note,
            portrait_emotions=portrait_emotions,
            character_state=character_state,
            episode_note=episode_note,
            life_note=life_note,
            space_note=space_note,
            pacing_note=pacing_note,
            reply_shape_note=reply_shape_note,
            max_tokens_cap=max_tokens_cap,
            expression_note=expression_note,
        ):
            kind = event["type"]
            if kind == "content":
                chunks.append(event["delta"])
                segments[-1] += event["delta"]
                if voice_builder is not None:
                    try:
                        voice_builder.feed(event["delta"])
                    except Exception as exc:  # noqa: BLE001
                        logger.warning("喂给语音切句失败（这条改走整段合成）：{}", exc)
                        voice_builder = None
                if voice_builder is None:
                    # 没走语音（或刚失败）→ 照旧逐字流式；顺手把攒着的文字补出去
                    if held:
                        held.append(event["delta"])
                        yield _sse("content", {"delta": "".join(held)})
                        held.clear()
                    else:
                        yield _sse("content", {"delta": event["delta"]})
                else:
                    held.append(event["delta"])
                    # 她已经合成好至少一段了 → 把攒着的文字放出去（D148：文字跟着语音走）
                    if voice_queue is not None and not voice_queue.empty():
                        while not voice_queue.empty():
                            voice_queue.get_nowait()
                        yield _sse("content", {"delta": "".join(held)})
                        held.clear()
                # D182：顺手把"已经生成的部分"落一次库（节流）—— 关窗再回来能看到说到哪了
                now = time.monotonic()
                if now - last_flush >= PROGRESS_FLUSH_SECONDS:
                    last_flush = now
                    # D197：挪线程 + 退避重试 —— 别让"撞锁 5 秒"堵死整个事件循环（手机端会整片卡住）
                    await _write_off_loop(_flush_progress, assistant_id, "".join(chunks), moods)
            elif kind == "reasoning":
                # 思考链只发给前端，不落库（D23）
                yield _sse("reasoning", {"delta": event["delta"]})
            elif kind == "mood":
                # D112/D113：每段的情绪标记 —— 语音那边按段贴，前端按段换立绘
                found = event.get("mood")
                if found:
                    moods.append(str(found))
                    mood_marks.append(
                        {"mood": str(found), "at": int(event.get("at") or 0)}
                    )
                if voice_builder is not None:
                    voice_builder.mark_mood(found)
                # D115：带上"这一刻在干净文本里的偏移" —— 前端按偏移对段，不会错位
                yield _sse("mood", {"mood": found, "at": event.get("at")})
            elif kind == "tool_call":
                record = {
                    "id": event.get("id"),
                    "name": event.get("name"),
                    "arguments": event.get("arguments"),
                }
                tool_calls.append(record)
                yield _sse("tool_call", record)
            elif kind == "tool_result":
                # 工具执行完毕（LLM协议§3.2）。前端只在【显示工具调用】打开时展示。
                yield _sse(
                    "tool_result",
                    {k: v for k, v in event.items() if k != "type" and v is not None},
                )
            elif kind == "segment_break":
                if segments[-1].strip():
                    segments.append("")
                    yield _sse("segment_break", {})
            elif kind == "state_delta":
                # V6.0-1：隐藏状态块。用户看不到；独立会话落库，失败不拖垮回复。
                delta = event.get("delta")
                if isinstance(delta, dict):
                    await _write_off_loop(
                        character_state_service.apply_delta_by_id,
                        friend.id,
                        delta,
                    )
            elif kind == "done":
                usage = event.get("usage") or {}
                stop_reason = event.get("stop_reason")
                mood = event.get("mood") or mood
                # 完整工具记录（含 ok / result / duration_ms）进 tools_used（D31）
                collected = event.get("tool_calls") or []
                if collected:
                    tool_calls = collected
    except (asyncio.CancelledError, GeneratorExit):
        # 用户按了停止 / 关掉页面：已生成的部分照样落库（api.md §1.6 / D30）
        if voice_builder is not None:
            voice_builder.abort()  # 没说完的回复不配音（片段还在合成缓存里，不浪费）
        await _write_off_loop(
            _finalize,
            assistant_id,
            "".join(chunks),
            message_service.PARTIAL,
            tool_calls,
            moods=_align_moods(segments, mood_marks, fallback=mood),
            segments=segments,
        )
        raise
    except Exception as exc:  # noqa: BLE001 —— 不假装成功（LLM协议§5.1）
        if voice_builder is not None:
            voice_builder.abort()
            voice_builder = None
        await _write_off_loop(
            _finalize,
            assistant_id,
            "".join(chunks),
            message_service.ERROR,
            tool_calls,
            mood=mood,
            moods=_align_moods(segments, mood_marks, fallback=mood),
            segments=segments,
        )
        code = "LLM_CALL_FAILED" if isinstance(exc, LLMCallError) else "INTERNAL_ERROR"
        # 报错必须落盘（只记错误，不记聊天明文）—— DB 里存不下原文，只能靠日志。
        # 用 ERROR 级别：日志窗口按级别筛，诊断信息的"最近错误"也只看 ERROR/CRITICAL。
        logger.error("本轮生成失败（{}）：{}", code, exc)
        # D148：攒着没放的文字先给用户（别让她"说了半句就没了"）
        if held:
            yield _sse("content", {"delta": "".join(held)})
            held.clear()
        yield _sse("error", {"code": code, "message": str(exc)})
    else:
        full_text = _store_text(segments, chunks)
        if bubble_max_chars:
            visible = _clean_segments(segments) or ([full_text] if full_text.strip() else [])
            split = bubble_splitter.split_segments(
                visible,
                max_chars=bubble_max_chars,
                max_bubbles=bubble_max_bubbles,
            )
            if split:
                segments = split
                full_text = "\n".join(split)
        if typo_rate > 0:
            segments = [typo_service.maybe_typo(item, rate=typo_rate) for item in segments]
            full_text = "\n".join(_clean_segments(segments))
        # 情绪对齐可以按“自然段”算，但**发送结构不动**：
        # 模型没写 <<<MSG>>> 时，一整段就还是一整段；只有 moods 按自然段对齐，
        # 这样沉浸模式逐段换表情，普通聊天/气泡不会被强行切碎。
        mood_segments = _clean_segments(segments)
        if len(mood_segments) <= 1:
            paragraphs = _paragraph_segments(full_text)
            if paragraphs:
                mood_segments = paragraphs
        aligned_moods = _align_moods(mood_segments, mood_marks, fallback=mood)
        # v4.1（D246）：一个字都没说出来（思考链吃光预算 / 模型空转）→ 不落「（空）」，按失败处理
        empty = not full_text.strip()
        await _write_off_loop(
            _finalize,
            assistant_id,
            full_text,
            message_service.ERROR if empty else message_service.COMPLETED,
            tool_calls,
            sources=_collect_sources(tool_calls),
            mood=mood,
            moods=aligned_moods,
            segments=segments,
        )
        if empty:
            logger.warning("这一轮她一个字都没说出来（思考链吃预算 / 模型空转）—— 按失败处理")
            yield _sse(
                "error",
                {"code": "LLM_EMPTY", "message": "她这轮没说出话来（可能思考链把预算吃光了）—— 再发一次就行"},
            )
        # v2（D85/D88）：配音在后台做
        launched = False
        try:
            launched = (
                voice_reply_service.launch_finish(voice_builder, full_text)
                if voice_builder is not None and not empty
                else False
            )
            if not launched:
                if not empty:
                    voice_reply_service.schedule(assistant_id)
        except Exception as exc:  # noqa: BLE001 —— 配音绝不许拖累聊天
            logger.warning("配音排队失败（不影响文字）：{}", exc)
        # v3.2（D148）：等后台把最后一段合出来 —— "每好一段，放一段文字"。
        # 有硬上限：超时 / 合成全失败 / 没排上，都立刻把剩下的文字补齐（一个字都不能丢）。
        if launched and voice_builder is not None and voice_queue is not None:
            event_loop = asyncio.get_running_loop()
            deadline = event_loop.time() + VOICE_RELEASE_TIMEOUT_SECONDS
            while True:
                try:
                    token = voice_queue.get_nowait()
                except asyncio.QueueEmpty:
                    if voice_builder.settled or event_loop.time() > deadline:
                        break
                    await asyncio.sleep(0.15)
                    continue
                if token is None:  # finish 收工哨兵
                    break
                if held:
                    yield _sse("content", {"delta": "".join(held)})
                    held.clear()
        if held:
            # 兜底：合成失败 / 超时 / 没排上后台合成 → 文字照样补齐
            yield _sse("content", {"delta": "".join(held)})
            held.clear()
        yield _sse(
            "done",
            {
                "user_message_id": user_message_id,
                "assistant_message_id": assistant_id,
                "usage": usage,
                "stop_reason": stop_reason,
                "segments": _clean_segments(segments),
                "moods": aligned_moods,
            },
        )
    finally:
        await _maybe_aclose(provider)


@router.post("/{friend_id}/messages")
async def send_message(
    friend_id: str,
    body: MessageCreate,
    session: Session = Depends(get_db),
    make_provider: Callable[..., ILLMProvider] = Depends(get_provider_factory),
) -> StreamingResponse:
    if not body.content.strip() and not body.image_id and not body.screen:
        raise ServiceError(
            "MESSAGE_EMPTY",
            "总要说点什么，发一张图，或者看一次屏",
            422,
        )
    friend = friend_service.get(session, Actor.USER, friend_id)
    # 先确认朋友存在，再要 provider：否则"没配 key"会盖掉"朋友不存在"
    # v3.5（D151）：这个朋友自己指定了模型端点就用它的（找不到会抛人话错误，不静默回落）
    provider = make_provider(friend.llm_base) if friend.llm_base else make_provider()
    # v2（T2.0-6 / D82）：带图的消息 → 先把 pending 里的图转正，再拿到编号
    image_rel: str | None = None
    image_no: int | None = None
    # v2.1（T2.1-5 / D100）：`screen: true` → 后端截屏，当成这条消息带的图（走发图那条链路）
    if body.screen:
        if not feature_flags.enabled("screen"):
            raise ServiceError(
                "FEATURE_DISABLED",
                "「看屏幕」这个功能现在是关着的 —— 去设置 → 功能里打开再用",
                409,
            )
        # D135 补充：看屏也要"能看图的模型" —— 两条都算：
        # ①「视觉跟随聊天」（截图直接 inline 给 chat 模型）
        # ② 单独配了视觉模型（截图走 loop 里的"转述"，跟我们自己发的图同一条路）
        settings_row_now = settings_service.get(session)
        if not (
            settings_service.vision_for_friend(settings_row_now, friend)
            or vision_service.configured(session)
        ):
            raise ServiceError(
                "VISION_NOT_READY",
                "看屏要一个能看图的模型（设置 → 模型 → 视觉：让它跟随聊天，或者单独配一个）",
                400,
            )
        captured = image_service.save_capture(screen_service.capture())
        image_rel = captured["rel"]
        image_no = message_service.next_image_no(session, friend_id)
    if body.image_id:
        image_rel = image_service.promote(body.image_id)
        if image_rel is None:
            raise ServiceError(
                "IMAGE_NOT_FOUND",
                "这张图找不到了（可能传完太久了）—— 重新发一张就行",
                400,
            )
        image_no = message_service.next_image_no(session, friend_id)
    if body.screen and not body.content.strip():
        body.content = "你看看我现在在干嘛。"
    # ⚠️ v3.14（D182）：上下文组装（历史 / 记忆 / 资料 / 看屏…）挪进后台 task 里做
    # —— 那一轮**不再挂在这次请求上**（关掉窗口也要继续说完）。
    mode_at_send = friend.current_mode

    user_message = message_service.create(
        session,
        friend_id,
        role="user",
        content=body.content,
        mode_at_send=mode_at_send,
        stream_status=message_service.COMPLETED,
        image_path=image_rel,
        image_no=image_no,
    )
    session.commit()  # 先把用户消息定下来，前端刷新也有据可依
    # v7.12（D341）：用户消息最高优先级，先暂停电脑端发起新的生活决策。
    # v7.20（D373）：不再直接 import life_service，改成发事件；
    # 生活线自己订阅 `chat.started`（见 features/subscribers.py）。
    feature_events.emit("chat.started", friend_id=friend_id)

    user_message_id = user_message.id
    # v7.13（D344）：同一朋友一次只跑一条回复；快速连发由前端防抖合并，
    # 这里再挡住手机/电脑同时发起的重叠回合。
    await turn_scheduler.acquire(friend_id)
    assistant = None
    try:
        assistant = message_service.create(
            session,
            friend_id,
            role="assistant",
            content="",
            mode_at_send=mode_at_send,
            stream_status=message_service.STREAMING,
        )
        session.commit()
        assistant_id = assistant.id
    except Exception:
        turn_scheduler.release(friend_id)
        raise
    # v3.10（P4 / D176）：这张图**先 commit 再**起后台任务给它生成"一句内容索引"
    # （D94 的教训：事务没提交就 spawn，后台任务读不到那条消息 / 那个文件）。
    if image_rel:
        image_note_service.spawn_describe(user_message_id)
    # v3.14（D182）：**她这一轮交给后台 task**，这里只当"订阅者"。
    # 客户端断开只是退订，task 照跑；要真打断得走 POST …/stop（用户按停止）。
    try:
        run = reply_runs.start(
            assistant_id=assistant_id,
            friend_id=friend_id,
            friend_name=friend.name,
            body=lambda run, wake: _run_reply(
                run,
                user_input=body.content,
                user_message_id=user_message_id,
                provider=provider,
            ),
        )
    except Exception:
        turn_scheduler.release(friend_id)
        raise
    # ⚠️ 就在这里订阅（不能等生成器第一次被迭代时才订阅 —— 见 reply_runs.subscriber 的注释）
    queue = run.subscribe()
    return StreamingResponse(
        reply_runs.subscriber(run, queue), media_type="text/event-stream", headers=dict(SSE_HEADERS)
    )


async def _run_reply(
    run: reply_runs.ReplyRun,
    *,
    user_input: str,
    user_message_id: str,
    provider: ILLMProvider,
) -> None:
    try:
        await _run_reply_body(
            run,
            user_input=user_input,
            user_message_id=user_message_id,
            provider=provider,
        )
    finally:
        turn_scheduler.release(run.friend_id)


async def _run_reply_body(
    run: reply_runs.ReplyRun,
    *,
    user_input: str,
    user_message_id: str,
    provider: ILLMProvider,
) -> None:
    """后台把这一轮跑完（D182）。

    **自己开 session**：请求级的那个在响应一结束就被 FastAPI 在别的线程里 commit/close，
    跟这里并发用同一个 Session 会出事（这也是 `history` 要在这儿重取一遍的原因）。
    组装完上下文后，剩下的活还是原来的 `sse_stream` —— 它负责落库 / 配音 / 收尾。
    """
    friend_id = run.friend_id
    with session_scope() as session:
        friend = friend_service.get(session, Actor.USER, friend_id)
        user_row = session.get(Message, user_message_id)
        user_image = user_row.image_path if user_row is not None else None
        # ⚠️ 本轮 user 消息已经落库了 → 从历史里剔掉，不然 user_input 会出现两遍
        history = [
            item
            for item in message_service.recent_for_context(
                session, friend_id, limit=HISTORY_LIMIT_DEFAULT
            )
            if item.id != user_message_id
        ]
        memories = memory_service.list_alive(session, friend_id)
        # V6.0-1（D296）：她此刻的持续状态。首次访问自动建默认行。
        character_state = character_state_service.get_or_create(session, friend_id)
        episode_note = episode_service.prompt_note(session, friend_id)
        life_note = (
            life_service.note_for_prompt(session, friend_id)
            if feature_flags.enabled("life")
            else ""
        )
        space_note = (
            space_service.prompt_note(session, friend_id)
            if feature_flags.enabled("space")
            else ""
        )
        pacing_decision = (
            pacing.choose_pacing(user_input, history)
            if getattr(friend, "chat_pacing_enabled", 0)
            else None
        )
        pacing_note = pacing.pacing_note(pacing_decision) if pacing_decision else None
        reply_shape_note = (
            pacing.reply_shape_note(pacing_decision) if pacing_decision else None
        )
        expression_note = (
            expression_service.note_for_prompt(session, friend_id)
            if feature_flags.enabled("expression")
            and getattr(friend, "humanizer_enabled", 0)
            else None
        )
        settings_row = settings_service.get(session)
        # chat 模型看不看得了图（D79/D83/D82）；v3.5（D151）：这个朋友自己指定了端点 → 按那个端点判
        inline_images = settings_service.vision_for_friend(settings_row, friend)
        # v2.1（T2.1-3）：她手头的资料 —— 清单永远带，相关片段按这句话去关键词命中（D98）
        if feature_flags.enabled("knowledge"):
            document_index = knowledge_service.document_index(session, friend_id)
            short_docs = knowledge_service.small_texts(session, friend_id)
            knowledge_hits = (
                [{"document_name": item["name"], "text": item["text"]} for item in short_docs]
                if short_docs
                else knowledge_service.retrieve(session, friend_id, user_input)
            )
        else:
            document_index = []
            knowledge_hits = []
        # v2.1（D100）：她「最近瞟到」的屏幕（定时看屏写的，半小时内有效）
        seen = screen_service.latest(friend_id) if feature_flags.enabled("screen") else None
        # 上一轮说话时的 mode 和现在不一致 → 用户刚切了模式，要让模型"立刻出戏/入戏"（D49）
        previous_mode = history[-1].mode_at_send if history else None
        # 2026-09-27（D197 续）：**上下文读完了，先把事务结掉再去流式生成**。
        # 这个 session 会在下面**陪着整轮回复**（可能几分钟）；如果它身上挂着事务，
        # 那几分钟里 SQLite 的锁就可能一直被它握着（手机端/进度落库全都会被噎住）。
        # `expire_on_commit=False`（见 get_session_factory）→ 提交后对象照样能用。
        session.commit()
        async for frame in sse_stream(
            friend=friend,
            user_input=user_input,
            provider=provider,
            history=history,
            memories=memories,
            user_message_id=user_message_id,
            assistant_id=run.assistant_id,
            deep_thinking=bool(settings_row.deep_thinking),
            session=session,
            previous_mode=previous_mode,
            user_image=user_image,
            inline_images=inline_images,
            knowledge_hits=knowledge_hits,
            document_index=document_index,
            screen_note=f"{seen['note']}\n{seen['text']}" if seen else None,
            portrait_emotions=(
                live2d_settings_service.emotion_labels_for_friend(
                    friend_id,
                    portrait_service.emotions(
                        friend_id,
                        portrait_pack_service.pack_id_for_friend(
                            friend_id,
                            getattr(friend, "portrait_pack_id", None),
                        ),
                    )
                    if feature_flags.enabled("portraits")
                    else [],
                    getattr(friend, "live2d_model_id", None),
                    getattr(friend, "visual_mode", "live2d"),
                )
            ),
            character_state=character_state,
            episode_note=episode_note,
            life_note=life_note,
            space_note=space_note,
            pacing_note=pacing_note,
            reply_shape_note=reply_shape_note,
            max_tokens_cap=pacing_decision.max_tokens if pacing_decision else None,
            bubble_max_chars=pacing_decision.bubble_chars if pacing_decision else None,
            bubble_max_bubbles=(
                pacing_decision.max_bubbles
                if pacing_decision
                else bubble_splitter.DEFAULT_MAX_BUBBLES
            ),
            expression_note=expression_note,
            typo_rate=(
                pacing_decision.typo_rate
                if pacing_decision and getattr(friend, "humanizer_enabled", 0)
                else 0.0
            ),
        ):
            run.publish(frame)
            # 2026-09-27（D197 续）：**这个会话要陪着整轮回复（可能几分钟）**，
            # 它身上只要挂着事务，SQLite 的锁就可能被它一直握着 —— 实测：探针点名
            # `长事务 20.1s（messages.py _run_reply）`，同时 `_finalize` 那边白等 5.5 秒后失败。
            # 所以**每帧之后都结束一次事务**（`expire_on_commit=False` → 提交后对象照样能用，
            # 下一帧的读取自己会开新事务）。工具轮自己的 commit 不受影响。
            if session.in_transaction():
                session.commit()
    feature_events.emit("chat.finished", friend_id=friend_id)
    # 收工：让所有开着的客户端刷新这一条（关掉的窗口、手机、桌宠都会跟上）
    await _announce_finished(run)


async def _announce_finished(run: reply_runs.ReplyRun) -> None:
    """D182：这轮跑完了 → 推一条 WS（跟主动消息同一条道，前端照着刷新历史）。

    为什么要它：关掉窗口的那个客户端**没连 SSE**，她不刷新就看不到完整回复。
    """
    try:
        with session_scope() as session:
            message = session.get(Message, run.assistant_id)
            if message is None:
                return
            payload = message_service.to_dict(message)
        await manager.broadcast(
            {
                "type": "new_message",
                "friend_id": run.friend_id,
                "friend_name": run.friend_name,
                "message": payload,
            }
        )
    except Exception as exc:  # noqa: BLE001 —— 推送失败不影响这一轮的结果
        logger.warning("回复跑完的 WS 推送失败（忽略）：{}", exc)


@router.post("/{friend_id}/messages/{message_id}/stop")
async def stop_message(
    friend_id: str,
    message_id: str,
    session: Session = Depends(get_db),
) -> dict:
    """用户按「停止」：**真的**把这一轮从后台掐掉（D182）。

    以前"停止"就是前端断开连接 —— 现在断开不再等于打断，所以必须有一个明确的停。
    （已生成的部分照旧落库标 `partial`，见 api.md §1.6。）
    """
    friend_service.get(session, Actor.USER, friend_id)
    message_service.get(session, friend_id, message_id)
    stopped = await reply_runs.stop(message_id)
    return {"ok": True, "stopped": stopped}


@router.post("/{friend_id}/draw_image")
async def draw_image(friend_id: str, body: dict, session: Session = Depends(get_db)) -> dict:
    """**手动画一张**（v3.1）：手机上那个「画图」面板调的。

    跟工具 `draw_image` 是一条路（`imagegen_service.generate`），区别只是：
    工具是**她自己决定**画（画完挂在她那条回复上），这个是**你点按钮**让她画
    —— 所以这里直接建一条**她的消息**，把图挂在上面（气泡里就是一张图）。

    body：`{prompt, ref_image_id?}`（ref_image_id = 刚上传那张参考图的 id，做图生图用）
    """
    friend_service.get(session, Actor.USER, friend_id)  # 先确认这个朋友存在
    from backend.services import imagegen_service  # noqa: PLC0415 —— 懒导入（它有网络依赖）

    payload = body if isinstance(body, dict) else {}
    prompt = str(payload.get("prompt") or payload.get("描述") or "").strip()
    if not prompt:
        raise ServiceError("IMAGE_PROMPT_EMPTY", "想画什么？写一句画面描述就行", 400)

    ref_png: bytes | None = None
    ref_id = str(payload.get("ref_image_id") or "").strip()
    if ref_id:
        path = image_service.path_for(ref_id)
        if path is None:
            raise ServiceError("IMAGE_NOT_FOUND", "参考图找不到了 —— 重新选一张", 400)
        try:
            ref_png = path.read_bytes()
        except OSError as exc:
            raise ServiceError("IMAGE_NOT_FOUND", f"参考图读不出来（{exc}）", 400) from exc

    result = await imagegen_service.generate(prompt, session=session, ref_png=ref_png)

    # 建成"她的一条消息"：图片挂在她气泡上（跟工具画完那条一模一样）
    message = message_service.create(
        session,
        friend_id,
        role="assistant",
        content="画好了。",
        mode_at_send=friend_service.get(session, Actor.USER, friend_id).current_mode,
        stream_status=message_service.COMPLETED,
        image_path=str(result["rel"]),
        image_no=message_service.next_image_no(session, friend_id),
    )
    session.commit()
    logger.info("手动画图完成：{}", result.get("model"))
    return {"message": message_service.to_dict(message), "model": result.get("model")}


@router.post("/{friend_id}/messages/{message_id}/speak")
def speak_message(
    friend_id: str,
    message_id: str,
    force: bool = Query(default=False, description="已经有音频也重新合成（文件被删时用）"),
    session: Session = Depends(get_db),
) -> dict:
    """给一条助手回复补语音（「念给我听」，api.md 的【v2】5）。

    **幂等**：已经有音频就直接返回现成的。**服务没开不是 500**：
    回 200 + 一句人话（D83）。
    """
    friend = friend_service.get(session, Actor.USER, friend_id)
    message = message_service.get(session, friend_id, message_id)
    if message.role != "assistant":
        raise ValueError("只有朋友的回复能配音")
    # 括号里的不念（D86）；整条只有动作 / 心理描写 → 本来就没话可说
    if not voice_reply_service.speakable_text(message.content):
        return {
            "ok": False,
            "hint": "这条只有动作或心里的想法，没有她要说的话",
            "nothing_to_say": True,
        }
    if not friend.voice_id:
        return {
            "ok": False,
            "hint": "这个朋友还没配音色 —— 去「设置 → 语音」里导入，再在「编辑人格 → 她的声音」里选一个",
        }
    if not force and voice_reply_service.has_audio(message):
        return {
            "ok": True,
            "audio_status": voice_reply_service.READY,
            "audio_url": voice_reply_service.audio_url(message),
            "audio_duration_ms": message.audio_duration_ms,
        }
    # 先看服务在不在：不在就当场说人话，别"排上队再失败"
    purposes = settings_service.effective_purposes_from(settings_service.get(session))
    tts_config = tts_service.tts_settings(purposes)
    status = tts_service.status(tts_config["base_url"])
    if not status["available"]:
        return {
            "ok": False,
            "hint": "语音服务（GPT-SoVITS）现在没开着 —— 打开它再点一次",
            "tts_status": status,
        }
    queued = voice_reply_service.schedule(message_id, force=True)
    return {"ok": True, "audio_status": voice_reply_service.PENDING, "queued": queued}


@router.get("/{friend_id}/messages/{message_id}/audio")
def get_message_audio(
    friend_id: str,
    message_id: str,
    session: Session = Depends(get_db),
) -> FileResponse:
    """取这条回复的语音（按 message_id 存的那份）。不在了就 404 + 人话。"""
    friend_service.get(session, Actor.USER, friend_id)
    message = message_service.get(session, friend_id, message_id)
    path = voice_reply_service.resolve_audio_path(message)
    if path is None:
        raise NotFoundError("AUDIO_NOT_FOUND", "这条的语音不在了 —— 点「重新合成」就行")
    # no-cache = 可以缓存但要回源确认（同一个 URL 可能被重新合成成新内容）
    return FileResponse(path, media_type="audio/wav", headers={"Cache-Control": "no-cache"})


@router.get("/{friend_id}/messages/{message_id}/audio/{index}")
def get_message_audio_segment(
    friend_id: str,
    message_id: str,
    index: int,
    session: Session = Depends(get_db),
):  # noqa: ANN201 —— 返回 FileResponse **或** StreamingResponse（联合类型 FastAPI 不认）
    """取**某一段**的语音（v2.2 / D105，沉浸模式一段一段说用）。不在了就 404 + 人话。

    v3.11（D178）：这段**还在流式合成就先"边写边发"**（首字节 ~2 秒就能听到她开口），
    写完的段照旧直接发完整文件（快路径）。
    """
    friend_service.get(session, Actor.USER, friend_id)
    message = message_service.get(session, friend_id, message_id)
    path = voice_reply_service.resolve_segment_path(message, index)
    if path is None:
        raise NotFoundError("AUDIO_NOT_FOUND", "这一段的语音不在了 —— 点「重新合成」就行")
    if voice_reply_service.is_streaming(message_id, index):
        return StreamingResponse(
            voice_reply_service.follow_segment(path, message_id, index),
            media_type="audio/wav",
            headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
        )
    return FileResponse(path, media_type="audio/wav", headers={"Cache-Control": "no-cache"})


@router.get("/{friend_id}/messages")
def list_messages(
    friend_id: str,
    limit: int = Query(default=50, ge=1, le=200),
    before: str | None = Query(default=None, description="上一页最老那条的 created_at"),
    session: Session = Depends(get_db),
) -> dict:
    """历史分页（api.md §2.2）。按时间正序返回，前端直接渲染。"""
    friend_service.get(session, Actor.USER, friend_id)
    messages, has_more = message_service.page(session, friend_id, limit=limit, before=before)
    return {
        "messages": [message_service.to_dict(m) for m in messages],
        "has_more": has_more,
    }
