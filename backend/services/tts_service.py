"""本地 GPT-SoVITS 的调用层（v2 / T2.0-4；决策见 D77 / D78）。

干三件事：

1. **健康检测**：`GET {base_url}/docs`，2 秒超时，**结果缓存 10 秒**（别每次点都打上游）
2. **权重切换**：后端记着"当前加载的是哪一对"，**只在变化时切**；
   并用**进程内锁**串行化——并发切换会把显存搞炸（用户自己打的那个"同路径跳过重载"补丁就是防这个）
3. **合成**：带缓存（同一句话、同一音色、同一套参数 → 不重复合成）

**我们不管它的进程**（D77 的决定）：GPT-SoVITS 是用户自己的整合包，光启动就要 60 秒。
连不上就**降级**——纯文字聊天完全不受影响，语音相关的入口给"人话提示"。

⚠️ **localhost 的请求必须绕开系统代理**（`trust_env=False`）：
用户开着梯子时，系统代理会把 127.0.0.1 的请求也吞掉（D52~D58 的教训）。
"""

from __future__ import annotations

import hashlib
import threading
import os
import time
import asyncio

import httpx
from loguru import logger

from backend import config
from backend.models import Voice
from backend.services.errors import ServiceError

# 用户点「测试/试听」时别每次都打上游
HEALTH_TTL_SECONDS = 10.0
HEALTH_TIMEOUT_SECONDS = 2.0
# 合成给足时间：**某个音色的"第一次"要把模型加载进显存，实测能超过 60 秒**
# （同一个音色第二次只要 2~3 秒）。所以这里放宽到 180 秒，
# 并且导入音色时会在后台先合成一次预热（下面 spawn_prewarm），让用户点"试听"时基本是秒回。
# v3.12（D180）：合成 ≈ 1:1 实时，用户常有一段 ~1 分钟的音频 → 180 秒不够用（他要求"两分钟都不为过"），
# 干脆放到 5 分钟；总时长上限（voice_reply_service 那个）也一起抬到 5 分钟。
SYNTH_TIMEOUT_SECONDS = 300.0

# 试听用的固定句子
PREVIEW_TEXT = "你好呀，我是你的朋友。今天过得怎么样？"

# 默认值（D77 实测那套：本地 9880 + 8 步）
DEFAULT_BASE_URL = "http://127.0.0.1:9880"
DEFAULT_SAMPLE_STEPS = 8

_client: httpx.Client | None = None
_lock = threading.Lock()  # 串行化"切权重 + 合成"
_health: dict = {"at": 0.0, "result": None}
_loaded: dict[str, str | None] = {"key": None}
# 后台预热任务的引用（不持有引用可能被 GC 掉）
_BACKGROUND: set[asyncio.Task] = set()  # type: ignore[type-arg]


def _http() -> httpx.Client:
    """本地小服务用的 client（连接复用；**不走系统代理**）。"""
    global _client
    if _client is None:
        _client = httpx.Client(
            timeout=httpx.Timeout(SYNTH_TIMEOUT_SECONDS, connect=5.0),
            trust_env=False,
        )
    return _client


def reset_cache() -> None:
    """测试用：清掉健康缓存 + "已加载权重"的记忆。"""
    _health["at"] = 0.0
    _health["result"] = None
    _loaded["key"] = None


def status(base_url: str, *, force: bool = False) -> dict:
    """服务在不在。结果缓存 10 秒（用户连点也不打上游）。"""
    now = time.monotonic()
    cached = _health["result"]
    if not force and cached is not None and now - float(_health["at"]) < HEALTH_TTL_SECONDS:
        return cached

    base = (base_url or "").rstrip("/")
    result: dict = {"available": False, "engine": "gpt-sovits", "base_url": base, "error": None}
    if not base:
        result["error"] = "还没填语音服务的地址"
    else:
        try:
            response = _http().get(
                f"{base}/docs", timeout=httpx.Timeout(HEALTH_TIMEOUT_SECONDS, connect=1.0)
            )
            result["available"] = response.status_code < 500
            if not result["available"]:
                result["error"] = f"服务回了 HTTP {response.status_code}"
        except httpx.HTTPError as exc:
            result["error"] = f"连不上（{type(exc).__name__}）——多半是语音服务没开着"

    _health["result"] = result
    _health["at"] = now
    return result


def _cache_key(text: str, voice: Voice, sample_steps: int) -> str:
    raw = "|".join(
        [
            text,
            voice.gpt_weights_path,
            voice.sovits_weights_path,
            voice.ref_audio_path,
            f"{voice.speed:.3f}",
            str(sample_steps),
        ]
    )
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()


def cache_name_of(*, text: str, voice: Voice, sample_steps: int = 8) -> str:
    """这次合成会落到哪个缓存文件名（不含 .wav）——给前端拼播放地址用。"""
    return _cache_key(text.strip(), voice, sample_steps)


def tts_settings(purposes: dict) -> dict:
    """从 `purposes.tts` 里取我们需要的几项（没填就给默认值）。

    默认值照 D77 实测那套：本地 9880 + 8 步。
    """
    conf = purposes.get("tts")
    conf = conf if isinstance(conf, dict) else {}
    raw_steps = conf.get("sample_steps")
    try:
        steps = int(raw_steps) if raw_steps else DEFAULT_SAMPLE_STEPS
    except (TypeError, ValueError):
        steps = DEFAULT_SAMPLE_STEPS
    return {
        "base_url": (conf.get("base_url") or DEFAULT_BASE_URL).rstrip("/"),
        "sovits_dir": conf.get("sovits_dir") or "",
        "sample_steps": min(64, max(1, steps)),
    }


def _ensure_weights(base_url: str, voice: Voice) -> None:
    """把服务切到这个音色；**同一对权重不重复切**（既慢又压显存）。

    ⚠️ v3.11（D179）：切之前**先看权重文件在不在**。为什么要多这一道：
    上游的 `set_*_weights` 会把路径**持久化进它自己的配置文件** ——
    拿一个"不存在的路径"去切，会把这个服务**永久弄坏**（下次启动直接崩，用户实测过）。
    文件不在就给人话报错，绝不拿它去切。
    """
    from pathlib import Path

    want = f"{voice.gpt_weights_path}|{voice.sovits_weights_path}"
    if _loaded["key"] == want:
        return
    for path, endpoint in (
        (voice.gpt_weights_path, "set_gpt_weights"),
        (voice.sovits_weights_path, "set_sovits_weights"),
    ):
        if not path or not Path(str(path)).is_file():
            raise ServiceError(
                "TTS_WEIGHTS_MISSING",
                f"这个音色的权重文件不在了（{Path(str(path or '？')).name}）—— "
                "去「设置 → 语音 → 音色库」重新导入一个",
                400,
            )
        try:
            response = _http().get(f"{base_url}/{endpoint}", params={"weights_path": path})
        except httpx.HTTPError as exc:
            raise ServiceError(
                "TTS_UNREACHABLE", f"语音服务连不上（{type(exc).__name__}）——多半是它没开着", 503
            ) from exc
        if response.status_code != 200:
            raise ServiceError(
                "TTS_WEIGHTS_FAILED",
                f"切音色失败：{response.text[:120]}",
                502,
            )
    _loaded["key"] = want
    logger.info("语音权重已切到：{}", voice.name)


def synthesize(*, text: str, voice: Voice, base_url: str, sample_steps: int = 8) -> bytes:
    """合成一段 wav。命中缓存就直接返回（同一句话不重复合成）。"""
    content = (text or "").strip()
    if not content:
        raise ServiceError("TTS_EMPTY_TEXT", "没有要念的内容")
    base = (base_url or "").rstrip("/")
    if not base:
        raise ServiceError("TTS_NO_BASE_URL", "还没填语音服务的地址", 400)
    # D77 实测：prompt_text 空着上游会直接 400（"cannot be empty when using SoVITS_V3"），
    # 所以在这里就拦住，给一句能看懂的话
    if not (voice.prompt_text or "").strip():
        raise ServiceError(
            "TTS_NO_PROMPT_TEXT", "这个音色没填参考音频的台词——合成就靠它，去音色库补上", 400
        )

    cached = config.TTS_CACHE_DIR / f"{_cache_key(content, voice, sample_steps)}.wav"
    if cached.exists():
        # **刷一下"最后使用时间"**：清理任务是按 mtime 判断"多久没人用过"的（D141），
        # 不刷的话天天用的那句话也会被当成老缓存删掉、下次白合成一遍。
        try:
            os.utime(cached, None)
        except OSError:  # 刷不动无所谓，别影响这次合成
            pass
        return cached.read_bytes()

    payload = {
        "text": content,
        "text_lang": "zh",
        "ref_audio_path": voice.ref_audio_path,
        "prompt_text": voice.prompt_text,
        "prompt_lang": "zh",
        "media_type": "wav",
        "streaming_mode": False,
        "text_split_method": "cut5",
        "sample_steps": sample_steps,
        "speed_factor": voice.speed,
    }
    with _lock:
        _ensure_weights(base, voice)
        try:
            response = _http().post(f"{base}/tts", json=payload)
        except httpx.HTTPError as exc:
            raise ServiceError(
                "TTS_UNREACHABLE", f"语音服务连不上（{type(exc).__name__}）——多半是它没开着", 503
            ) from exc
        if response.status_code != 200:
            raise ServiceError("TTS_FAILED", f"合成失败：{response.text[:140]}", 502)
        raw = response.content

    try:
        config.TTS_CACHE_DIR.mkdir(parents=True, exist_ok=True)
        cached.write_bytes(raw)
    except OSError:  # 缓存写不进去也不能让这次合成失败
        logger.warning("语音缓存写不进去（忽略）：{}", cached)
    return raw


# ---------- v3.11：流式合成（边收边写，前端可以"边生成边播"）----------
#
# 为什么：合成本身 ≈ 1:1 实时（实测 6 字 2.0s / 25 字 6.0s / 60 字 11.1s），
# 而一段长台词要整段合成完才播 → 用户干等十几秒。
# 上游 `streaming_mode=true` 是**首块给 WAV 头、之后给裸 PCM**，所以我们可以：
#   ① 先写一个"数据长度未知"的占位头（0xFFFFFFFF）→ 前端拿到就能开始播；
#   ② 边收边往后追加 PCM；
#   ③ 收完把占位头改回真实长度 → 缓存文件就是标准 wav（后面拼接/时长都用它）。
# ⚠️ 占位头期间**不能让 wave 模块读这个文件**（读不了），所以"修头"必须在收尾前完成。

_WAV_PLACEHOLDER = 0xFFFFFFFF
# ⚠️ 只在上游连头都没给时兜底。**正常必须读上游的**（D180 的教训：我原来写死 32000，
# 而实测上游流的是 **48000** → 所有流式音频都以 0.667 倍速播 → 听感低一个音区，
# 用户报"所有音色都变成大叔音"。）
STREAM_SAMPLE_RATE_FALLBACK = 32000
# 上游连 WAV 头都没给时的兜底格式（正常永远走 `parse_wav_format`）
_WAV_FMT_FALLBACK = {"rate": STREAM_SAMPLE_RATE_FALLBACK, "channels": 1, "width": 2}


def parse_wav_format(chunk: bytes) -> dict[str, int] | None:
    """从一段 WAV 头里读出 (rate / channels / width)，读不出给 None。"""
    import struct

    if len(chunk) < 36 or chunk[:4] != b"RIFF" or chunk[8:12] != b"WAVE":
        return None
    try:
        channels, rate = struct.unpack_from("<HI", chunk, 22)  # fmt 块：22=声道, 24=采样率
        bits = struct.unpack_from("<H", chunk, 34)[0]  # 34=位宽
    except struct.error:
        return None
    if not (rate and channels and bits):
        return None
    return {"rate": int(rate), "channels": int(channels), "width": max(1, bits // 8)}


def _wav_header(*, rate: int, data_bytes: int, channels: int = 1, width: int = 2) -> bytes:
    """手写 WAV 头（data_bytes = 0xFFFFFFFF 表示"还不知道多长"）。"""
    import struct

    byte_rate = rate * channels * width
    return b"".join(
        [
            b"RIFF",
            struct.pack("<I", 36 + data_bytes if data_bytes != _WAV_PLACEHOLDER else _WAV_PLACEHOLDER),
            b"WAVEfmt ",
            struct.pack("<IHHIIHH", 16, 1, channels, rate, byte_rate, channels * width, width * 8),
            b"data",
            struct.pack("<I", data_bytes),
        ]
    )


def fix_wav_header(path) -> int:  # noqa: ANN001
    """把占位头改成真实长度；返回 data 字节数（0 = 文件不对，别当成功）。"""
    from pathlib import Path

    target = Path(path)
    try:
        raw = target.read_bytes()
    except OSError:
        return 0
    if len(raw) < 44 or raw[:4] != b"RIFF":
        return 0
    data_bytes = max(0, len(raw) - 44)
    import struct

    fmt = parse_wav_format(raw) or dict(_WAV_FMT_FALLBACK)
    header = _wav_header(
        rate=fmt["rate"], data_bytes=data_bytes, channels=fmt["channels"], width=fmt["width"]
    )
    try:
        with target.open("r+b") as fh:
            fh.seek(0)
            fh.write(header)
    except OSError:
        return 0
    return data_bytes


def synthesize_stream_to(
    *,
    text: str,
    voice: Voice,
    base_url: str,
    sample_steps: int,
    path,  # noqa: ANN001 —— Path
    opener=None,  # noqa: ANN001 —— 测试注入：返回一个"逐块吐 bytes"的可迭代对象
) -> int:
    """流式合成并**边收边写** `path`。返回写入的 PCM 字节数（0 = 失败）。

    失败一律返回 0（上层按"这段没合成出来"处理）；**绝不抛**。
    """
    from pathlib import Path

    content = (text or "").strip()
    target = Path(path)
    if not content:
        return 0
    base = (base_url or "").rstrip("/")
    if not base or not (voice.prompt_text or "").strip():
        return 0
    payload = {
        "text": content,
        "text_lang": "zh",
        "ref_audio_path": voice.ref_audio_path,
        "prompt_text": voice.prompt_text,
        "prompt_lang": "zh",
        "media_type": "wav",
        "streaming_mode": True,  # ← 关键：边生成边给
        "text_split_method": "cut5",
        "sample_steps": sample_steps,
        "speed_factor": voice.speed,
    }
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        logger.warning("语音分段目录建不了（{}）：{}", target.parent, exc)
        return 0

    written = 0
    try:
        with _lock:
            _ensure_weights(base, voice)
            chunks = iter(opener if opener is not None else _stream_chunks(base, payload))
            # v3.12（D180）：**头按上游说的写**（它才 48000；我原来写死 32000 → 0.667 倍速 = 大叔音）
            head = next(chunks, b"")
            parsed = parse_wav_format(head)
            fmt = parsed or dict(_WAV_FMT_FALLBACK)
            with target.open("wb") as fh:
                fh.write(
                    _wav_header(
                        rate=fmt["rate"],
                        data_bytes=_WAV_PLACEHOLDER,
                        channels=fmt["channels"],
                        width=fmt["width"],
                    )
                )
                if head and parsed is None:
                    # 上游连头都没给（纯 PCM）→ 这一块也是数据
                    fh.write(head)
                    fh.flush()
                    written += len(head)
                for chunk in chunks:
                    if not chunk:
                        continue
                    fh.write(chunk)
                    fh.flush()  # 立刻落盘：另一端可能正在读这个文件
                    written += len(chunk)
    except ServiceError:
        raise
    except Exception as exc:  # noqa: BLE001 —— 流式失败只影响这段
        logger.warning("流式合成失败：{}", exc)
        return 0
    return written if written > 0 else 0


def _stream_chunks(base: str, payload: dict):  # noqa: ANN001
    """真正去上游拉流（逐块 yield）；连不上就抛 ServiceError。"""
    try:
        with _http().stream("POST", f"{base}/tts", json=payload) as response:
            if response.status_code != 200:
                raise ServiceError(
                    "TTS_FAILED", f"合成失败：{response.read()[:140].decode('utf-8', 'ignore')}", 502
                )
            for chunk in response.iter_bytes():
                yield chunk
    except httpx.HTTPError as exc:
        raise ServiceError(
            "TTS_UNREACHABLE", f"语音服务连不上（{type(exc).__name__}）——多半是它没开着", 503
        ) from exc


def cache_file(name: str):
    """按缓存文件名取音频（给 `/api/tts/audio/<name>` 用）。

    只认 40 位十六进制——防止有人拿 `../` 乱翻文件。
    """
    stem = name.removesuffix(".wav")
    if len(stem) != 40 or any(c not in "0123456789abcdef" for c in stem):
        return None
    path = config.TTS_CACHE_DIR / f"{stem}.wav"
    return path if path.exists() else None


def _prewarm(voice_id: str, base_url: str, sample_steps: int) -> None:
    """后台把试听那句先合成一遍：第一次的"加载模型"代价就藏在这儿了。"""
    from backend.db import session_scope
    from backend.models import Voice

    try:
        with session_scope() as session:
            voice = session.get(Voice, voice_id)
            if voice is None:
                return
            synthesize(text=PREVIEW_TEXT, voice=voice, base_url=base_url, sample_steps=sample_steps)
        logger.info("语音预热完成（试听会秒回）")
    except Exception as exc:  # noqa: BLE001 —— 预热失败绝不能影响任何事
        logger.warning("语音预热失败（不影响使用）：{}", exc)


def spawn_prewarm(voice_id: str, base_url: str, sample_steps: int = DEFAULT_SAMPLE_STEPS) -> None:
    """导入音色后调一次：后台预热（只在事件循环里调用；没有循环就跳过）。"""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    task = loop.create_task(asyncio.to_thread(_prewarm, voice_id, base_url, sample_steps))
    _BACKGROUND.add(task)
    task.add_done_callback(_BACKGROUND.discard)


async def cancel_prewarms() -> None:
    for task in list(_BACKGROUND):
        task.cancel()
    _BACKGROUND.clear()


# ---------- 缓存清理（v3.0 / D141）----------

# 多久没用过的缓存可以删
CACHE_KEEP_DAYS = 3
# 缓存总量上限（超了就从最旧的开始删）
CACHE_MAX_BYTES = 300 * 1024 * 1024


def purge_cache(
    *,
    keep_days: int = CACHE_KEEP_DAYS,
    max_bytes: int = CACHE_MAX_BYTES,
    now: float | None = None,
) -> dict[str, int]:
    """清合成缓存：先按"多久没被用过"，再按总量从最旧删起。返回 `{"removed", "freed"}`。

    缓存是**纯加速**用的：删了只是下次同一句话要重新合成一遍（几秒 / 一点点钱）。
    所以这里可以放心按"3 天没动过"就删 —— mtime 在每次命中缓存时会被刷（见 `synthesize`）。
    """
    directory = config.TTS_CACHE_DIR
    if not directory.is_dir():
        return {"removed": 0, "freed": 0}
    current = now if now is not None else time.time()
    cutoff = current - keep_days * 86400

    removed = 0
    freed = 0
    survivors = []  # [(path, mtime, size)] 先留着，等会儿按总量决定还删不删
    for path in sorted(directory.glob("*.wav")):
        try:
            stat = path.stat()
        except OSError:  # 正好被别的线程删了
            continue
        if keep_days > 0 and stat.st_mtime < cutoff:
            try:
                path.unlink()
            except OSError:
                continue
            removed += 1
            freed += stat.st_size
        else:
            survivors.append((path, stat.st_mtime, stat.st_size))

    # 还是太大 → 从最旧的开始删，直到降到上限以下
    total = sum(size for _path, _mtime, size in survivors)
    if total > max_bytes:
        for path, _mtime, size in sorted(survivors, key=lambda item: item[1]):
            if total <= max_bytes:
                break
            try:
                path.unlink()
            except OSError:
                continue
            total -= size
            removed += 1
            freed += size

    if removed:
        logger.info("清理语音合成缓存：{} 个 / {:.1f} MB", removed, freed / 1024 / 1024)
    return {"removed": removed, "freed": freed}
