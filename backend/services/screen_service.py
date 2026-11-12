"""看屏（v2.1 / T2.1-5 / D100）。

两件事：
1. **截图 + 交给视觉模型写一句观察**（手动看屏 / 定时看屏都用它）
2. **记住"你现在开着谁"（focus）** 和**每个朋友"最近看到的"**

口径（用户定的）：
· 定时看屏**只给当前 focus 的那个朋友**看（3 分钟没心跳 = 没人）——
  一堆朋友一起看屏幕太恐怖，也费 token
· 看屏结果是"瞟了一眼"：一句话，**不主动刷消息**；下次说话时贴进 system
· 截图会发给云端视觉模型（提示词里也要求她**别复述敏感内容**）
"""

from __future__ import annotations

import base64
import io
import json
import time
from datetime import datetime, timezone

from loguru import logger

from backend.llm.errors import LLMConfigError
from backend.llm.factory import create_provider_for_purpose
from backend.services import settings_service
from backend.services.errors import ServiceError

FOCUS_TTL_SECONDS = 180.0  # 3 分钟没心跳就算"没人"
OBSERVATION_TTL_SECONDS = 1800.0  # 观察 30 分钟内有效
CAPTURE_MAX_SIDE = 2048

OBSERVE_PROMPT = """这是 TA 电脑屏幕的画面。用**一句话**说说「他现在大概在干什么」
（在写代码 / 在打游戏 / 在看视频 / 在聊天 / 在看文档……）。

要求：
- 只写这一句，别复述屏幕上的文字，别评价，别用「这张图」这种说法
- 看到密码、聊天记录这类内容**不要写出来**，只说「在处理一些文字/消息」就行
- 拿不准就说「看着像在忙别的事」"""

_focus: dict = {"friend_id": None, "at": 0.0}
_latest: dict[str, dict] = {}


def set_focus(friend_id: str | None) -> None:
    _focus["friend_id"] = friend_id or None
    _focus["at"] = time.monotonic()


def get_focus() -> dict:
    fresh = _focus["friend_id"] is not None and (time.monotonic() - _focus["at"]) <= FOCUS_TTL_SECONDS
    return {
        "friend_id": _focus["friend_id"] if fresh else None,
        "fresh": fresh,
        "at": (
            datetime.fromtimestamp(
                time.time() - (time.monotonic() - _focus["at"]), tz=timezone.utc
            ).isoformat()
            if _focus["at"]
            else None
        ),
    }


def set_latest(friend_id: str, text: str) -> None:
    _latest[friend_id] = {"text": text.strip(), "at": time.monotonic()}


def latest(friend_id: str, *, max_age_seconds: float = OBSERVATION_TTL_SECONDS) -> dict | None:
    """该朋友"最近看到的"（过期就不给了）。"""
    record = _latest.get(friend_id)
    if not record or not record.get("text"):
        return None
    age = time.monotonic() - float(record.get("at") or 0)
    if age > max_age_seconds:
        return None
    minutes = max(1, int(age // 60))
    return {"text": record["text"], "age_seconds": age, "note": f"（{minutes} 分钟前看到的）"}


def reset_state() -> None:
    """测试用。"""
    _focus.update({"friend_id": None, "at": 0.0})
    _latest.clear()


def capture() -> bytes:
    """截一张全屏（多屏一起截），并压到最长边 ≤2048。**纯本地，不发网**。"""
    try:
        from PIL import ImageGrab  # noqa: PLC0415

        image = ImageGrab.grab(all_screens=True)
    except Exception as exc:  # noqa: BLE001 —— 没桌面 / 权限问题都算"截不到"
        raise ServiceError("SCREEN_CAPTURE_FAILED", f"截不到屏（{type(exc).__name__}）", 500) from exc
    if max(image.size) > CAPTURE_MAX_SIDE:
        image.thumbnail((CAPTURE_MAX_SIDE, CAPTURE_MAX_SIDE))
    buffer = io.BytesIO()
    image.convert("RGB").save(buffer, format="PNG")
    return buffer.getvalue()


async def observe(png: bytes, *, session) -> str:  # noqa: ANN001 —— Session（避免循环 import）
    """把截图交给视觉模型，要一句"他在干什么"。没配视觉 → 人话错误（D83）。"""
    purposes = settings_service.effective_purposes_from(settings_service.get(session))
    try:
        provider = create_provider_for_purpose(json.dumps(purposes, ensure_ascii=False), "vision")
    except LLMConfigError as exc:
        raise ServiceError(
            "VISION_NOT_READY",
            f"看屏要一个能看图的模型（设置 → 模型 → 视觉）：{exc}",
            400,
        ) from exc
    encoded = base64.b64encode(png).decode()
    messages = [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": OBSERVE_PROMPT},
                {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{encoded}"}},
            ],
        }
    ]
    chunks: list[str] = []
    try:
        # D116：思考常开的模型会吃掉预算，给正文留够
        async for event in provider.chat_stream(messages, temperature=0.2, max_tokens=500):
            if event.get("type") == "content":
                chunks.append(event.get("delta") or "")
            elif event.get("type") == "done":
                break
    finally:
        closer = getattr(provider, "aclose", None)
        if closer is not None:
            await closer()
    text = "".join(chunks).strip()
    if not text:
        raise ServiceError("SCREEN_OBSERVE_EMPTY", "看是看到了，但说不出在干什么（再试一次）", 502)
    logger.info("看屏：{}", text[:60])
    return text
