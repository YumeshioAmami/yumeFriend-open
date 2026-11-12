"""情节 / 理解 / 纠正（V6.0-3 / D297）。

memories 记事实；这里记“我们之间发生过什么”和“我上次怎么误解了你”。
所有条目按 friend_id 隔离，删除朋友时由 FK CASCADE 清掉。
"""

from __future__ import annotations

import json
import re
import uuid
from datetime import datetime, timezone
from typing import Any, Iterable, Mapping

from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.models import FriendEpisode


VALID_KINDS = ("episode", "insight", "correction")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _new_id() -> str:
    return str(uuid.uuid4())


def _loads(raw: str | None, default: Any) -> Any:
    try:
        value = json.loads(raw or "")
    except (TypeError, ValueError):
        return default
    return value if isinstance(value, type(default)) else default


def _dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False)


def _text(value: Any, *, limit: int = 800) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()[:limit]


def _string_list(value: Any, *, limit: int = 8) -> list[str]:
    if not isinstance(value, (list, tuple)):
        return []
    out: list[str] = []
    for item in value:
        text = _text(item, limit=300)
        if text and text not in out:
            out.append(text)
        if len(out) >= limit:
            break
    return out


def list_recent(
    session: Session,
    friend_id: str,
    *,
    limit: int = 20,
    kinds: Iterable[str] | None = None,
) -> list[FriendEpisode]:
    stmt = select(FriendEpisode).where(
        FriendEpisode.friend_id == friend_id,
        FriendEpisode.deleted_at.is_(None),
    )
    if kinds:
        allowed = [kind for kind in kinds if kind in VALID_KINDS]
        if allowed:
            stmt = stmt.where(FriendEpisode.kind.in_(allowed))
    stmt = stmt.order_by(FriendEpisode.occurred_at.desc()).limit(max(1, limit))
    return list(session.execute(stmt).scalars())


def save(
    session: Session,
    friend_id: str,
    *,
    kind: str,
    summary: str,
    meaning: str | None = None,
    tone: str | None = None,
    unresolved: str | None = None,
    importance: float = 0.5,
    confidence: float = 0.5,
    evidence: Iterable[str] | None = None,
    source_message_ids: Iterable[str] | None = None,
    occurred_at: str | None = None,
) -> FriendEpisode:
    if kind not in VALID_KINDS:
        raise ValueError(f"非法 episode kind：{kind}")
    summary = _text(summary, limit=1000)
    if not summary:
        raise ValueError("episode summary 不能为空")

    # 完全相同的摘要不重复写（宁可少记，也不刷屏）
    existing = session.execute(
        select(FriendEpisode).where(
            FriendEpisode.friend_id == friend_id,
            FriendEpisode.kind == kind,
            FriendEpisode.summary == summary,
            FriendEpisode.deleted_at.is_(None),
        )
    ).scalar_one_or_none()
    if existing is not None:
        return existing

    now = _now()
    row = FriendEpisode(
        id=_new_id(),
        friend_id=friend_id,
        kind=kind,
        summary=summary,
        meaning=_text(meaning) or None,
        tone=_text(tone, limit=80) or None,
        unresolved=_text(unresolved, limit=500) or None,
        importance=max(0.0, min(1.0, float(importance))),
        confidence=max(0.0, min(1.0, float(confidence))),
        evidence=_dumps(_string_list(evidence)),
        source_message_ids=_dumps(_string_list(source_message_ids)),
        occurred_at=occurred_at or now,
        created_at=now,
    )
    session.add(row)
    session.flush()
    return row


def to_dict(row: FriendEpisode) -> dict[str, Any]:
    return {
        "id": row.id,
        "friend_id": row.friend_id,
        "kind": row.kind,
        "summary": row.summary,
        "meaning": row.meaning,
        "tone": row.tone,
        "unresolved": row.unresolved,
        "importance": float(row.importance),
        "confidence": float(row.confidence),
        "evidence": _loads(row.evidence, []),
        "source_message_ids": _loads(row.source_message_ids, []),
        "occurred_at": row.occurred_at,
    }


def prompt_note(session: Session, friend_id: str, *, limit: int = 12) -> str:
    """给下一步对话的自然语言块：不展示 kind / confidence 这些内部字段名。"""
    rows = list_recent(session, friend_id, limit=limit)
    if not rows:
        return ""

    episodes = [row for row in rows if row.kind == "episode"][:4]
    insights = [row for row in rows if row.kind == "insight"]
    corrections = [row for row in rows if row.kind == "correction"][:3]
    sure = [row for row in insights if float(row.confidence) >= 0.6][:4]
    unsure = [row for row in insights if float(row.confidence) < 0.6][:3]

    def line(row: FriendEpisode) -> str:
        text = row.summary
        if row.meaning:
            text += f"（对你来说：{row.meaning}）"
        if row.unresolved:
            text += f"（还挂着：{row.unresolved}）"
        return f"- {text}"

    parts: list[str] = []
    if episodes:
        parts.append("# 你们之间\n" + "\n".join(line(row) for row in episodes))
    if sure or corrections:
        lines = [line(row) for row in corrections] + [line(row) for row in sure]
        parts.append("# 你目前对他的理解\n" + "\n".join(lines))
    if unsure:
        parts.append("# 你还不确定的事\n" + "\n".join(line(row) for row in unsure))
    return "\n\n".join(parts)
