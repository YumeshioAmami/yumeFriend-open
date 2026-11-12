"""给每张图存一句**内容索引**（P4 / D176）。

**解决什么**（用户实测的原话）："之前模型反映过想找图片但是找不到，因为图片只有编号没有日期，
他不知道去哪里找我说的图片。"
→ 占位符原来只有 `[图片#12 · 09-23]`：按时间勉勉强强，**按内容完全找不到**。
给每张图存一句"一只橘猫趴在键盘上"之后：占位符能带上它、`list_images` 能列出来、
`look_image(query="猫")` 也能按它去找。

三条纪律（跟 `avatar_description` 一模一样）：
  · **best-effort**：没配能看图的模型 / 调用失败 / 超时 → 静默跳过，**绝不影响发消息**
  · **异步**：后台任务，别让发消息等一次 LLM 调用（⚠️ D94：调用方必须**先 commit 再 spawn**）
  · **一句话**：只存描述本身（别评价、别猜），上限截断，免得把占位符撑爆
"""

from __future__ import annotations

import asyncio
import base64
import json
from pathlib import Path

from loguru import logger

from backend.db import session_scope
from backend.llm.errors import LLMConfigError
from backend.llm.factory import create_provider_for_purpose
from backend.models import Message
from backend.services import image_service, settings_service

# 这段提示词在 docs/prompts.md 的【v3.10】里有解释（别两处漂移）
DESCRIBE_PROMPT = """用**一句**中文说清楚这张图里有什么（这是给看不见图的人用的索引）：
先说是什么 / 谁，再说在做什么、或最显眼的东西。只写你确实看到的 —— 别评价、别猜、别加解释。"""

MAX_TOKENS = 400  # 思考常开的模型要留够正文预算
MAX_NOTE_CHARS = 80  # 索引就是一句话；太长会把占位符撑爆

# 后台任务的引用（不持有引用的话可能被 GC 掉）
_BACKGROUND: set[asyncio.Task] = set()  # type: ignore[type-arg]


async def describe_image(provider, image_path: str | Path) -> str:  # noqa: ANN001
    """把一张图交给（能看图的）provider，要一句内容索引。"""
    raw = Path(image_path).read_bytes()
    suffix = str(image_path).lower()
    media = "image/png" if suffix.endswith(".png") else "image/webp" if suffix.endswith(".webp") else "image/jpeg"
    url = f"data:{media};base64,{base64.b64encode(raw).decode()}"
    messages = [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": DESCRIBE_PROMPT},
                {"type": "image_url", "image_url": {"url": url}},
            ],
        }
    ]
    chunks: list[str] = []
    async for event in provider.chat_stream(messages, temperature=0.2, max_tokens=MAX_TOKENS):
        if event.get("type") == "content":
            chunks.append(event.get("delta") or "")
        elif event.get("type") == "done":
            break
    text = "".join(chunks).strip().replace("\n", " ")
    return text[:MAX_NOTE_CHARS]


async def describe_and_store(message_id: str) -> bool:
    """读那条消息上的图 → 生成索引 → 存回 `messages.image_note`。返回是否成功。"""
    with session_scope() as session:
        row = session.get(Message, message_id)
        if row is None or not row.image_path or (row.image_note or "").strip():
            return False  # 没图 / 已经看清过了 → 什么都不做
        path = image_service.abs_path(row.image_path)
        purposes = settings_service.effective_purposes_from(settings_service.get(session))
        purposes_json = json.dumps(purposes, ensure_ascii=False)

    if path is None or not path.is_file():
        return False
    try:
        # 「视觉」跟随聊天（默认）→ 拿到的就是聊天模型（flash 自带图像理解）；
        # 单独配了视觉模型 → 用它。两种都行，代码一样。
        provider = create_provider_for_purpose(purposes_json, "vision")
    except LLMConfigError as exc:
        logger.info("没有能看图的模型，跳过图片索引（不影响任何事）：{}", exc)
        return False

    try:
        note = await describe_image(provider, path)
        if not note:
            return False
        with session_scope() as session:
            row = session.get(Message, message_id)
            if row is None:
                return False
            row.image_note = note
            session.flush()
        logger.info("图片索引已生成：{}", note[:40])
        return True
    except Exception as exc:  # noqa: BLE001 —— 失败绝不能影响发消息
        logger.warning("图片索引生成失败（不影响使用）：{}", exc)
        return False
    finally:
        closer = getattr(provider, "aclose", None)
        if closer is not None:
            await closer()


def spawn_describe(message_id: str) -> None:
    """后台起一个索引任务（只在事件循环里调用；没有循环就静默跳过）。"""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return  # 同步上下文（纯单测 / 脚本）→ 跳过，反正它是 best-effort
    task = loop.create_task(describe_and_store(message_id))
    _BACKGROUND.add(task)
    task.add_done_callback(_BACKGROUND.discard)


async def cancel_describes() -> None:
    """进程退出时收尾。"""
    for task in list(_BACKGROUND):
        task.cancel()
    for task in list(_BACKGROUND):
        try:
            await task
        except (asyncio.CancelledError, Exception):  # noqa: BLE001
            pass
    _BACKGROUND.clear()
