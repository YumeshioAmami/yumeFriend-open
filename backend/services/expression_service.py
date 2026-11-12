"""从 TA 最近的说法里抽短表达，作为说话样本（v7.13 / D344）。

这里刻意不做“人格覆盖”：只学短词组/口头说法，作为样本注入；不自动改 personality。
第一版不调用额外模型，纯频率 + 过滤，够便宜也够稳。
"""

from __future__ import annotations

import re
import uuid
from collections import Counter
from datetime import datetime, timezone

from loguru import logger
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from backend.models import Friend, FriendExpression, Message

MAX_EXPRESSIONS = 8
SCAN_MESSAGES = 120

_STOPWORDS = {
    "我们", "你们", "他们", "这个", "那个", "什么", "怎么", "为什么", "就是",
    "可以", "不是", "真的", "一个", "没有", "自己", "现在", "然后", "因为",
    "所以", "但是", "还是", "已经", "觉得", "知道", "可能", "应该", "如果",
    "今天", "明天", "昨天", "感觉", "有点", "一点", "这种", "那种", "东西",
}
_PHRASE_RE = re.compile(r"[\u4e00-\u9fff]{2,8}")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _phrases(text: str) -> list[str]:
    out: list[str] = []
    for chunk in re.split(r"[，。！？!?；;、\s,.]+", text or ""):
        for match in _PHRASE_RE.findall(chunk):
            if match in _STOPWORDS:
                continue
            # 去掉“我觉得/你这种”这类通用前缀
            for prefix in ("我觉得", "我感觉", "其实", "然后", "就是", "真的"):
                if match.startswith(prefix) and len(match) > len(prefix) + 1:
                    match = match[len(prefix) :]
            if 2 <= len(match) <= 8:
                out.append(match)
    return out


def refresh(session: Session, friend: Friend) -> int:
    rows = session.execute(
        select(Message.content)
        .where(Message.friend_id == friend.id, Message.role == "user")
        .order_by(Message.created_at.desc())
        .limit(SCAN_MESSAGES)
    ).scalars()
    counts: Counter[str] = Counter()
    for content in rows:
        counts.update(_phrases(content or ""))
    picked = [
        (phrase, score)
        for phrase, score in counts.most_common(MAX_EXPRESSIONS * 3)
        if score >= 2
    ][:MAX_EXPRESSIONS]
    session.execute(delete(FriendExpression).where(FriendExpression.friend_id == friend.id))
    now = _now()
    for phrase, score in picked:
        session.add(
            FriendExpression(
                id=str(uuid.uuid4()),
                friend_id=friend.id,
                style=phrase,
                situation="",
                score=float(score),
                source="auto",
                created_at=now,
                updated_at=now,
            )
        )
    session.flush()
    return len(picked)


def note_for_prompt(session: Session, friend_id: str) -> str:
    rows = list(
        session.execute(
            select(FriendExpression)
            .where(FriendExpression.friend_id == friend_id)
            .order_by(FriendExpression.score.desc())
            .limit(MAX_EXPRESSIONS)
        ).scalars()
    )
    if not rows:
        return ""
    return (
        "# 他最近常这样说\n"
        + "；".join(f"「{row.style}」" for row in rows)
        + "\n（这些只是他的说话习惯，不要逐句复读。）"
    )


def tick() -> int:
    from backend.db import session_scope

    total = 0
    try:
        with session_scope() as session:
            friends = list(
                session.execute(
                    select(Friend).where(
                        Friend.deleted_at.is_(None),
                        Friend.humanizer_enabled == 1,
                    )
                ).scalars()
            )
            for friend in friends:
                total += refresh(session, friend)
    except Exception as exc:  # noqa: BLE001
        logger.warning("表达学习失败（已吞掉）：{}", exc)
    return total
