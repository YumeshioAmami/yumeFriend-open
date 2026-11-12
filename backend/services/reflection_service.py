"""停歇反思（“梦”，V6.0-3 / D297）。

不是编故事，也不是写摘要：把一段真实对话整理成
  · episode：发生了什么
  · insight：这说明了什么（有证据、有置信度）
  · correction：我上次怎么误解了他、他怎么纠正的
并更新角色状态 / open loops。

触发点复用“停歇”语义：默认 5 分钟没有新消息才跑。
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from typing import Any

from loguru import logger
from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.models import Friend, Message
from backend.services import character_state_service, episode_service, settings_service


REFLECTION_IDLE_SECONDS = 300
REFLECTION_MAX_MESSAGES = 80
REFLECTION_MIN_MESSAGES = 4


REFLECTION_SYSTEM = """你在帮一个 AI 朋友整理刚才和 TA 的对话。
你不是在写摘要；你是在形成她真正会带进下一次对话的“经历”和“理解”。

只输出一个 JSON 对象，不要 markdown 代码围栏、不要解释、不要正文：
{
  "episodes": [
    {"summary":"发生了什么（带情境，不要逐句摘要）","meaning":"这对她/你们意味着什么","tone":"情绪底色","unresolved":"还挂着什么（可空）","importance":0.6,"confidence":0.8,"evidence":["对话里的短引文"],"source_message_ids":["消息 id"]}
  ],
  "insights": [
    {"summary":"关于 TA 或你们关系的一条理解","meaning":"为什么这么判断","confidence":0.6,"evidence":["短引文"],"source_message_ids":["消息 id"]}
  ],
  "corrections": [
    {"summary":"我上次误解了什么；TA 怎么纠正的","meaning":"下次应该怎么改","confidence":0.8,"evidence":["短引文"],"source_message_ids":["消息 id"]}
  ],
  "state_delta": {
    "appraisal":"一句话说清这段对话对她意味着什么",
    "affect_delta":{"valence":0.0,"arousal":0.0,"security":0.0,"connection":0.0},
    "energy":0.55,
    "intent":"靠近/逗/怼/哄/观察/沉默/解释/问清楚/躲开",
    "inner_note":"她私下的第一人称短句",
    "relationship_note":"她此刻对你们关系的感受",
    "open_loops_add":["还没说完/没解决的事"],
    "open_loops_resolve":["已经放下的事"]
  }
}

规则：
· 只写真实发生过的事；没有证据就不要写。
· evidence 用对话里的短引文；source_message_ids 用给你的消息 id。
· 不确定的写低 confidence；宁可少写，也不要编。
· 不要写“用户是一个敏感的人”这种星座式标签；写具体行为/情境。
· 不要重复已经记过的内容。
· 没有 episodes/insights/corrections 时给空数组。"""


def _parse_json(text: str) -> dict[str, Any]:
    raw = (text or "").strip()
    if raw.startswith("```"):
        raw = raw.split("\n", 1)[-1]
        if raw.rstrip().endswith("```"):
            raw = raw.rstrip()[:-3].rstrip()
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        try:
            payload, _ = json.JSONDecoder().raw_decode(raw)
        except json.JSONDecodeError as exc:
            raise ValueError(f"反思输出不是合法 JSON：{exc}") from exc
    if not isinstance(payload, dict):
        raise ValueError("反思输出必须是 JSON 对象")
    return payload


def _parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


def _messages_since(session: Session, friend_id: str, watermark: str | None) -> list[Message]:
    if watermark:
        stmt = (
            select(Message)
            .where(Message.friend_id == friend_id, Message.created_at > watermark)
            .order_by(Message.created_at)
            .limit(REFLECTION_MAX_MESSAGES)
        )
        return list(session.execute(stmt).scalars())
    rows = list(
        session.execute(
            select(Message)
            .where(Message.friend_id == friend_id)
            .order_by(Message.created_at.desc())
            .limit(REFLECTION_MAX_MESSAGES)
        ).scalars()
    )
    return list(reversed(rows))


def build_reflection_messages(
    friend: Friend,
    messages: list[Message],
    state_note: str,
    known: list[dict[str, Any]],
) -> list[dict[str, str]]:
    transcript = "\n".join(
        f"[id={message.id}] {'TA' if message.role == 'user' else '你'}：{message.content}"
        for message in messages
    )
    known_text = "\n".join(
        f"- {item.get('kind')}: {item.get('summary')}" for item in known[:20]
    ) or "（还没有）"
    user = (
        f"【当前状态】\n{state_note or '（无）'}\n\n"
        f"【已经记过的（不要重复）】\n{known_text}\n\n"
        f"【刚才的对话】\n{transcript}"
    )
    return [
        {"role": "system", "content": REFLECTION_SYSTEM},
        {"role": "user", "content": user},
    ]


def _prepare_reflection(
    session: Session,
    friend: Friend,
    *,
    idle_seconds: int = REFLECTION_IDLE_SECONDS,
    now: datetime | None = None,
) -> dict[str, Any] | None:
    messages = _messages_since(session, friend.id, friend.last_reflection_at)
    if len(messages) < REFLECTION_MIN_MESSAGES:
        return None

    state = character_state_service.get_or_create(session, friend.id)
    known = [episode_service.to_dict(row) for row in episode_service.list_recent(session, friend.id, limit=20)]
    prompt = build_reflection_messages(
        friend,
        messages,
        character_state_service.to_prompt(state),
        known,
    )
    return {
        "prompt": prompt,
        "scanned": len(messages),
        "occurred_at": messages[-1].created_at,
    }


async def _call_reflection_provider(
    provider: Any, prompt: list[dict[str, str]]
) -> dict[str, Any]:
    chunks: list[str] = []
    async for event in provider.chat_stream(prompt, temperature=0.2, max_tokens=1400):
        if event.get("type") == "content":
            chunks.append(event.get("delta") or "")
        elif event.get("type") == "done":
            break
        elif event.get("type") == "error":
            detail = str(
                event.get("error")
                or event.get("message")
                or (event.get("payload") or {}).get("message")
                or "未知错误"
            )
            raise ValueError(f"反思模型报错：{detail}")
    text = "".join(chunks)
    if not text.strip():
        raise ValueError("反思模型没有返回内容（空回复）")
    return _parse_json(text)


def _apply_reflection(
    session: Session,
    friend: Friend,
    payload: dict[str, Any],
    occurred_at: str,
) -> dict[str, int]:
    counts = {"episodes": 0, "insights": 0, "corrections": 0}
    for kind, field in (
        ("episode", "episodes"),
        ("insight", "insights"),
        ("correction", "corrections"),
    ):
        items = payload.get(field)
        if not isinstance(items, list):
            continue
        for item in items[:3]:
            if not isinstance(item, dict):
                continue
            try:
                episode_service.save(
                    session,
                    friend.id,
                    kind=kind,
                    summary=str(item.get("summary") or ""),
                    meaning=str(item.get("meaning") or ""),
                    tone=str(item.get("tone") or ""),
                    unresolved=str(item.get("unresolved") or ""),
                    importance=float(item.get("importance") or (0.7 if kind != "episode" else 0.5)),
                    confidence=float(item.get("confidence") or 0.5),
                    evidence=item.get("evidence") or [],
                    source_message_ids=item.get("source_message_ids") or [],
                    occurred_at=occurred_at,
                )
                counts[field] += 1
            except Exception as exc:  # noqa: BLE001 —— 单条坏不许拖垮整次反思
                logger.warning("反思条目写入失败（{}）：{}", kind, exc)

    state_delta = payload.get("state_delta")
    if isinstance(state_delta, dict):
        character_state_service.apply_delta(session, friend.id, state_delta)

    friend.last_reflection_at = occurred_at
    session.flush()
    return counts


async def run_reflection(
    session: Session,
    friend: Friend,
    provider: Any,
    *,
    idle_seconds: int = REFLECTION_IDLE_SECONDS,
    now: datetime | None = None,
) -> dict[str, int]:
    prepared = _prepare_reflection(
        session, friend, idle_seconds=idle_seconds, now=now
    )
    if prepared is None:
        return {"scanned": 0, "episodes": 0, "insights": 0, "corrections": 0}
    payload = await _call_reflection_provider(provider, prepared["prompt"])
    counts = _apply_reflection(
        session, friend, payload, prepared["occurred_at"]
    )
    return {"scanned": prepared["scanned"], **counts}


def _find_candidates(session: Session, *, idle_seconds: int, now: datetime) -> list[Friend]:
    candidates: list[Friend] = []
    friends = list(
        session.execute(select(Friend).where(Friend.deleted_at.is_(None))).scalars()
    )
    for friend in friends:
        last = session.execute(
            select(Message)
            .where(Message.friend_id == friend.id)
            .order_by(Message.created_at.desc())
            .limit(1)
        ).scalar_one_or_none()
        if last is None:
            continue
        last_at = _parse_time(last.created_at)
        if last_at is None or (now - last_at).total_seconds() < idle_seconds:
            continue
        watermark = _parse_time(friend.last_reflection_at)
        if watermark is not None and last_at <= watermark:
            continue
        candidates.append(friend)
    return candidates


async def _maybe_aclose(provider: Any) -> None:
    closer = getattr(provider, "aclose", None)
    if closer is None:
        return
    try:
        await closer()
    except Exception:  # noqa: BLE001
        logger.warning("关闭反思 provider 失败（忽略）")


async def tick(*, idle_seconds: int = REFLECTION_IDLE_SECONDS, now: datetime | None = None) -> int:
    from backend.db import session_scope
    from backend.llm.factory import create_provider

    now = now or datetime.now(timezone.utc)
    with session_scope() as session:
        candidates = _find_candidates(session, idle_seconds=idle_seconds, now=now)
    if not candidates:
        return 0

    handled = 0
    for friend in candidates:
        provider = None
        prepared: dict[str, Any] | None = None
        friend_name = friend.name
        # D319：只在这个短事务里读上下文 / 建 provider；LLM 等待期间不持有 session。
        with session_scope() as session:
            fresh = session.get(Friend, friend.id)
            if fresh is None or fresh.deleted_at is not None:
                continue
            friend_name = fresh.name
            try:
                prepared = _prepare_reflection(
                    session, fresh, idle_seconds=idle_seconds, now=now
                )
                if prepared is None:
                    continue
                bases, active = settings_service.llm_config(session)
                provider = create_provider(bases, active)
            except Exception as exc:  # noqa: BLE001
                logger.warning("反思跳过（{} 准备失败）：{}", fresh.name, exc)
                continue
        if prepared is None or provider is None:
            await _maybe_aclose(provider)
            continue
        try:
            payload = await _call_reflection_provider(provider, prepared["prompt"])
            with session_scope() as session:
                fresh = session.get(Friend, friend.id)
                if fresh is None or fresh.deleted_at is not None:
                    continue
                counts = _apply_reflection(
                    session, fresh, payload, prepared["occurred_at"]
                )
            handled += 1
            logger.info(
                "停歇反思：朋友={} 看了 {} 条，情节 {} / 理解 {} / 纠正 {}",
                friend_name,
                prepared["scanned"],
                counts["episodes"],
                counts["insights"],
                counts["corrections"],
            )
        except Exception as exc:  # noqa: BLE001 —— 下轮再试
            logger.warning("停歇反思失败（下轮会重试）：{}", exc)
        finally:
            await _maybe_aclose(provider)
    return handled
