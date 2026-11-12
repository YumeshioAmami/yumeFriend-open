"""停歇补漏：聊完一段之后，回头把值得记的补上（D24 / architecture.md §5.2）。

**为什么必须有**：同步写入（回复时模型顺手吐工具块）**会漏** ——
实测连说三句"我在学 Rust / 下周去日本出差 / 我妈寄了橘子"，模型**一次工具都没调**。
所以停歇后要用同一条对话再"提取"一遍。

水位线 `friends.last_catchup_at`：只提取这之后的新消息，**保证不重复提取**（幂等）。
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Sequence

from loguru import logger
from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.agent import tools as tools_module
from backend.agent.layer_rules import LAYER_DECISION_RULES
from backend.agent.parser import ToolBlockParser
from backend.config import CATCHUP_MAX_MESSAGES
from backend.llm.base import ILLMProvider
from backend.models import Friend, Message
from backend.services import memory_service

EXTRACTION_SYSTEM = (
    """你在帮一个 AI 朋友整理记忆。输入是它和用户刚才的一段对话。

请把**值得长期记住**的事实用工具块输出，一行一个：
<tool>{"name":"save_memory","arguments":{"content":"要记的内容","tag":"normal","layer":"long_term","importance":0.5}}</tool>

判断标准：
- 值得记：用户的偏好、经历、计划、承诺、重要事实（名字/生日/工作/家人/宠物）、关系变化
- 不值得记：寒暄、玩笑、一次性的情绪、你已经在上下文里看到过的旧事
- 宁缺勿滥：拿不准就别记。没有值得记的就**什么都不要输出**。

只输出工具块，不要输出任何解释或正文。

"""
    # 和聊天时用的是**同一份**选层规则，避免两处说法不一样
    + LAYER_DECISION_RULES
)


def _parse(iso: str | None) -> datetime | None:
    if not iso:
        return None
    try:
        parsed = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _find_candidates(session: Session, *, idle_seconds: int, now: datetime) -> list[Friend]:
    """哪些朋友"聊完停歇了、且有新消息没整理"。"""
    candidates: list[Friend] = []
    for friend in session.execute(select(Friend).where(Friend.deleted_at.is_(None))).scalars():
        last = session.execute(
            select(Message.created_at)
            .where(Message.friend_id == friend.id)
            .order_by(Message.created_at.desc())
            .limit(1)
        ).scalar_one_or_none()
        last_at = _parse(last)
        if last_at is None:
            continue  # 还没聊过
        if (now - last_at).total_seconds() < idle_seconds:
            continue  # 还在聊，别插队
        watermark = _parse(friend.last_catchup_at)
        if watermark is not None and last_at <= watermark:
            continue  # 这段已经整理过了（水位线生效）
        candidates.append(friend)
    return candidates


def _messages_since(session: Session, friend_id: str, watermark: str | None) -> list[Message]:
    """水位线之后的消息；**第一次整理（没有水位线）只看最近的 N 条**。

    不去翻远古记录：一是没必要，二是首轮把几十条老对话整个丢给模型又慢又费。
    """
    if watermark:
        stmt = (
            select(Message)
            .where(Message.friend_id == friend_id, Message.created_at > watermark)
            .order_by(Message.created_at)
            .limit(CATCHUP_MAX_MESSAGES)
        )
        return list(session.execute(stmt).scalars())

    recent = list(
        session.execute(
            select(Message)
            .where(Message.friend_id == friend_id)
            .order_by(Message.created_at.desc())
            .limit(CATCHUP_MAX_MESSAGES)
        ).scalars()
    )
    return list(reversed(recent))


def build_extraction_messages(
    messages: Sequence[Message], known: Sequence[str] = ()
) -> list[dict[str, str]]:
    lines = []
    for message in messages:
        speaker = "用户" if message.role == "user" else "朋友"
        lines.append(f"{speaker}：{message.content}")

    # ⚠️ 把"已经记过的"一并给它，**从源头避免重复提取**（D28 的思路）。
    # 只靠字符串相似度去重会漏：实测 "用户叫天海梦汐" 和 "用户的名字是天海梦汐"
    # 相似度 0.67 < 0.85，就变成两条重复了。
    known_block = ""
    if known:
        listed = "\n".join(f"- {item}" for item in known[:80])
        known_block = f"\n\n【它已经记过这些了，**不要重复输出**】\n{listed}"

    return [
        {"role": "system", "content": EXTRACTION_SYSTEM},
        {
            "role": "user",
            "content": "[对话开始]\n" + "\n".join(lines) + "\n[对话结束]" + known_block,
        },
    ]


async def run_catchup(session: Session, friend: Friend, provider: ILLMProvider) -> dict[str, Any]:
    """对某个朋友跑一次补漏。返回统计；**失败会抛**（交给调度器记日志）。"""
    messages = _messages_since(session, friend.id, friend.last_catchup_at)
    if not messages:
        return {"scanned": 0, "saved": 0, "merged": 0}

    known = [m.content for m in memory_service.list_alive(session, friend.id)]
    parser = ToolBlockParser()
    calls: list[dict[str, Any]] = []
    async for event in provider.chat_stream(
        build_extraction_messages(messages, known), temperature=0.3, max_tokens=1200
    ):
        if event.get("type") == "content":
            calls.extend(p for p in parser.feed(event.get("delta") or "") if p["type"] == "tool_call")
        elif event.get("type") == "done":
            break
    calls.extend(p for p in parser.flush() if p["type"] == "tool_call")

    saved = merged = 0
    for call in calls[:10]:  # 防爆：一次最多写 10 条
        record = tools_module.execute(session, friend.id, call)
        if record["ok"]:
            saved += 1
            if (record.get("result") or {}).get("merged"):
                merged += 1
        else:
            logger.warning("补漏时 {} 没写成：{}", record.get("name"), record.get("error"))

    # 水位线推到最后一条**看见过**的消息 —— 下次从这里往后取，不重复
    friend.last_catchup_at = messages[-1].created_at
    session.commit()
    return {"scanned": len(messages), "saved": saved, "merged": merged}


async def tick(*, idle_seconds: int | None = None, now: datetime | None = None) -> int:
    """调度器每 30 秒调一次。返回这次处理了几个朋友。"""
    from backend.config import CATCHUP_IDLE_SECONDS
    from backend.db import session_scope
    from backend.llm.factory import create_provider, create_provider_for_llm_base
    from backend.services import settings_service

    now = now or datetime.now(timezone.utc)
    idle = CATCHUP_IDLE_SECONDS if idle_seconds is None else idle_seconds
    handled = 0
    with session_scope() as session:
        candidates = _find_candidates(session, idle_seconds=idle, now=now)
    if not candidates:
        return 0

    for friend in candidates:
        with session_scope() as session:
            fresh = session.get(Friend, friend.id)
            if fresh is None:
                continue
            # v3.5（D151）：这个朋友的补漏也走**她自己的**模型端点（没配就全局聊天用途）
            name = (fresh.llm_base or "").strip()
            try:
                if name:
                    row = settings_service.get(session)
                    provider = create_provider_for_llm_base(row.llm_bases, name)
                else:
                    bases, active = settings_service.llm_config(session)
                    provider = create_provider(bases, active)
            except Exception as exc:  # noqa: BLE001 —— 没配 provider 就跳过这个人
                logger.warning("记忆补漏跳过（{} 拿不到 provider）：{}", fresh.name, exc)
                continue
            try:
                stats = await run_catchup(session, fresh, provider)
                handled += 1
                if stats["scanned"]:
                    logger.info(
                        "记忆补漏：朋友={} 看了 {} 条，写入 {} 条（合并 {}）",
                        fresh.name,
                        stats["scanned"],
                        stats["saved"],
                        stats["merged"],
                    )
            except Exception as exc:  # noqa: BLE001 —— 定时任务不许把调度器搞死
                logger.warning("记忆补漏失败（下轮会重试）：{}", exc)
            finally:
                await _maybe_aclose(provider)
    return handled


async def _maybe_aclose(provider: ILLMProvider) -> None:
    closer = getattr(provider, "aclose", None)
    if closer is not None:
        try:
            await closer()
        except Exception:  # noqa: BLE001
            pass
