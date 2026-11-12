"""独立视觉模型的"看图转述"（D135 / D90 的已知缺口补上）。

**解决什么**：聊天模型看不了图（或者用户给「视觉」单独配了另一个模型）时，
原来只会跟她说"有张图你看不到" → 现在**把图发给视觉模型，让她把内容转述成文字**，
再把这段文字注入上下文（她就能聊图了）。

· 只在**独立配了视觉模型**（`purposes.vision` 有 base_url + key）时生效；
  视觉跟聊天同一路（跟随聊天）时**不走这里**（那种情况图直接 inline，D90）。
· 描述**按文件缓存**（同一个文件只花一次钱）；进程内缓存，重启即失效（无所谓）。
· 失败一律返回 None（上层退回"看不到"那句人话），**绝不抛**。
"""

from __future__ import annotations

import base64
import time
from pathlib import Path
from typing import Any

import httpx
from loguru import logger

from backend.services.errors import ServiceError

TIMEOUT_SECONDS = 60.0
PROMPT = (
    "请用两句中文客观描述这张图片的内容（这是给一位看不到图片的朋友转述用的）："
    "先说她/他/它是什么、在做什么，再说画面里显眼的东西。只描述你确实看到的，别猜、别评价。"
)

_cache: dict[tuple[str, float], str] = {}


def _conf(session) -> dict[str, str]:  # noqa: ANN001
    from backend.services import settings_service  # noqa: PLC0415

    purposes = settings_service.effective_purposes_from(settings_service.get(session))
    vision = purposes.get("vision") if isinstance(purposes.get("vision"), dict) else {}
    chat = purposes.get("chat") if isinstance(purposes.get("chat"), dict) else {}
    return {
        "base_url": str(vision.get("base_url") or "").rstrip("/"),
        "api_key": str(vision.get("api_key") or ""),
        "model": str(vision.get("model") or ""),
        "follow_chat": "1" if vision.get("follow_chat") else "0",
        "chat_model": str(chat.get("model") or ""),
    }


def configured(session) -> bool:  # noqa: ANN001
    """独立视觉模型配好了吗（没配 / 是"跟随聊天" → 都算没配）。"""
    conf = _conf(session)
    if conf["follow_chat"] == "1":
        return False
    return bool(conf["base_url"] and conf["api_key"] and conf["model"])


def cached(path: str | None) -> str | None:
    """只看缓存（不联网）—— 历史里的图用它，别为了看图卡住聊天。"""
    if not path:
        return None
    try:
        key = (path, Path(path).stat().st_mtime)
    except OSError:
        return None
    return _cache.get(key)


async def describe(session, path: str | None) -> str | None:  # noqa: ANN001
    """拿这张图的"转述"（有缓存就直接用）。失败/没配 → None。"""
    if not path:
        return None
    hit = cached(path)
    if hit is not None:
        return hit
    conf = _conf(session)
    if conf["follow_chat"] == "1" or not (conf["base_url"] and conf["api_key"]):
        return None
    try:
        raw = Path(path).read_bytes()
    except OSError:
        return None
    media = "image/png" if path.lower().endswith(".png") else "image/jpeg"
    url = f"data:{media};base64,{base64.b64encode(raw).decode()}"
    body = {
        "model": conf["model"] or conf["chat_model"],
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": PROMPT},
                    {"type": "image_url", "image_url": {"url": url}},
                ],
            }
        ],
        "temperature": 0.2,
    }
    started = time.monotonic()
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT_SECONDS, trust_env=False) as client:
            response = await client.post(
                f"{conf['base_url']}/chat/completions",
                headers={"Authorization": f"Bearer {conf['api_key']}"},
                json=body,
            )
        response.raise_for_status()
        data = response.json()
        text = str(((data.get("choices") or [{}])[0].get("message") or {}).get("content") or "").strip()
    except (httpx.HTTPError, ValueError, KeyError, IndexError) as exc:
        logger.warning("视觉转述失败（退回'看不到'）：{}", exc)
        return None
    if not text:
        return None
    logger.info("视觉转述完成：{:.1f}s / {} 字", time.monotonic() - started, len(text))
    try:
        _cache[(path, Path(path).stat().st_mtime)] = text
    except OSError:
        pass
    return text


__all__ = ["ServiceError", "cached", "configured", "describe"]