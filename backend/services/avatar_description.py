"""「她知道自己的样子」（D84）：上传头像后**看一次图、写成两三句**，之后只注入文字。

为什么不每轮喂图：一张图约 1000~1500 token，聊 30 轮就是 30 次（D82 那个坑的翻版）。

三条纪律：
  · **best-effort**：没配视觉模型 / 模型挂了 / 超时 → 静默跳过，绝不影响上传和聊天
  · **异步**：走后台任务，别让上传接口等一次 LLM 调用
  · **用户能改**：生成的是"草稿"，用户在人格弹窗里可以随便改（personality 归用户）
"""

from __future__ import annotations

import asyncio
import base64
import json

from loguru import logger

from backend.db import session_scope
from backend.llm.errors import LLMConfigError
from backend.llm.factory import create_provider_for_purpose
from backend.models import Friend
from backend.services import avatar_service, settings_service

# 这段提示词在 docs/prompts.md 的【v2】6 里有解释（别两处漂移）
DESCRIBE_PROMPT = """这是 TA 给你选的头像。用两三句话写下"你的样子"：

- 如果图里是一个人物：说清外貌（发色、瞳色、气质、常穿什么），像自我介绍那样
- 如果图里不是人物（风景 / 抽象画 / 物件 / 动物）：写它给人的感觉，别硬说成"我长这样"
- 只写描述本身：别加解释，别用"这张图"这种说法
"""

MAX_TOKENS = 520  # D116：思考常开的模型要留够正文预算

# 后台任务的引用（不持有引用的话可能被 GC 掉）
_BACKGROUND: set[asyncio.Task] = set()  # type: ignore[type-arg]


async def describe_avatar(provider, image_bytes: bytes) -> str:  # noqa: ANN001
    """把一张图交给（多模态）provider，要一段"你的样子"的文字。"""
    encoded = base64.b64encode(image_bytes).decode()
    messages = [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": DESCRIBE_PROMPT},
                {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{encoded}"}},
            ],
        }
    ]
    chunks: list[str] = []
    async for event in provider.chat_stream(messages, temperature=0.3, max_tokens=MAX_TOKENS):
        if event.get("type") == "content":
            chunks.append(event.get("delta") or "")
        elif event.get("type") == "done":
            break
    return "".join(chunks).strip()


async def describe_and_store(friend_id: str) -> bool:
    """读头像 → 生成描述 → 存回 `friends.avatar_description`。返回是否成功。"""
    with session_scope() as session:
        friend = session.get(Friend, friend_id)
        if friend is None:
            return False
        path = avatar_service.file_path(friend)
        if path is None:
            # 多半是"上传那边还没提交就开始跑后台任务"（踩过一次，见 T2.0-9）——
            # 留一行日志，别让它再静默失败
            logger.info("还看不到头像文件，跳过「她的样子」（不影响上传）")
            return False  # 没头像就没什么可看的
        image_bytes = path.read_bytes()
        purposes = settings_service.effective_purposes_from(settings_service.get(session))
        purposes_json = json.dumps(purposes, ensure_ascii=False)

    try:
        provider = create_provider_for_purpose(purposes_json, "vision")
    except LLMConfigError as exc:
        logger.info("没配视觉模型，跳过头像描述（不影响任何事）：{}", exc)
        return False

    try:
        text = await describe_avatar(provider, image_bytes)
        if not text:
            return False
        with session_scope() as session:
            row = session.get(Friend, friend_id)
            if row is None:
                return False
            row.avatar_description = text
            session.flush()
        logger.info("「她的样子」已生成：{}", text[:36])
        return True
    except Exception as exc:  # noqa: BLE001 —— 描述失败绝不能影响任何事
        logger.warning("「她的样子」生成失败（不影响使用）：{}", exc)
        return False
    finally:
        closer = getattr(provider, "aclose", None)
        if closer is not None:
            await closer()


def spawn_describe(friend_id: str) -> None:
    """后台起一个描述任务（只在事件循环里调用；没有循环就静默跳过）。"""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return  # 同步上下文（纯单测 / 脚本）→ 跳过，反正它是 best-effort
    task = loop.create_task(describe_and_store(friend_id))
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
