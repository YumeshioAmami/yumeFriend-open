"""主动消息的“开口理由”仲裁（v7.15 / D351）。

以前主动消息分成两套：
  · 生活线朋友只走 `friend_life.pending_share`；
  · 其他朋友只走旧 heartbeat 的随机冒泡。

这会让“她为什么现在找你”变得很单一。这里统一收集真实候选，再由
`proactive_service` 决定说 / 不说。候选只来自已经落库的事实，不凭空造话题。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.models import Friend, FriendState, Message
from backend.services import (
    character_state_service,
    episode_service,
    life_service,
    memory_service,
    message_service,
    thought_service,
)


INITIATIVE_COOLDOWN_SECONDS = 15 * 60
RECENT_MENTION_LIMIT = 8
_EMPTY_SHARE_MARKERS = (
    "没翻到值得看",
    "什么都没看到",
    "等于什么都没看到",
    "全是之前看过",
    "没有新东西",
    "没新东西",
    "白忙一场",
    "bilibili；哔哩哔哩",
    "黄历",
    "日历",
    "天气预报",
    "浏览器环境",
    "正在验证",
    "百度百科",
    "汉语字典",
    "播放页",
)


@dataclass(frozen=True)
class InitiativeCandidate:
    friend: Friend
    kind: str
    content: str
    hint: str
    score: float
    source_id: str | None = None


def _parse(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def last_proactive_at(session: Session, friend_id: str) -> datetime | None:
    raw = session.execute(
        select(Message.created_at)
        .where(Message.friend_id == friend_id, Message.is_proactive == 1)
        .order_by(Message.created_at.desc())
        .limit(1)
    ).scalar_one_or_none()
    return _parse(raw)


def _recent_assistant_texts(session: Session, friend_id: str) -> list[str]:
    rows = message_service.recent_for_context(
        session, friend_id, limit=RECENT_MENTION_LIMIT
    )
    return [str(getattr(row, "content", "") or "") for row in rows if row.role == "assistant"]


def _recently_mentioned(session: Session, friend_id: str, text: str) -> bool:
    needle = (text or "").strip()
    if not needle:
        return False
    return any(
        memory_service.similarity(needle, previous) >= 0.78
        for previous in _recent_assistant_texts(session, friend_id)
    )


def _thought_hint(content: str) -> str:
    return (
        f"[系统提示] 你心里惦记着要跟他说一件事：{content}\n"
        "**先看你们最近的对话**：如果他刚提过这事已经办完了、或你们已经聊过并有结论了，"
        "就别再提（那会像失忆）—— 这次只回 `[沉默]` 就行。\n"
        "否则就用你自己的话说出来（一句话就够，别像在念待办）。"
    )


def _open_loop_hint(text: str) -> str:
    return (
        f"[系统提示] 你心里还挂着一件事：{text}\n"
        "如果现在想提，就用你自己的方式开个头；一句就够，别像汇报。"
        "不想提就只回 `[沉默]`。"
    )


def _episode_hint(summary: str, unresolved: str) -> str:
    tail = f"（还挂着：{unresolved}）" if unresolved else ""
    return (
        f"[系统提示] 你们之间还有一件没完全放下的事：{summary}{tail}\n"
        "如果现在想接着聊，就自然提一句；不想提就只回 `[沉默]`，不要为了找话题硬提。"
    )


def share_text_is_worthwhile(text: str) -> bool:
    """D353：主动分享的文本至少要有真实内容，不能只是“白忙一场”的占位。"""
    cleaned = (text or "").strip()
    if len(cleaned) < 12:
        return False
    return not any(marker in cleaned for marker in _EMPTY_SHARE_MARKERS)


def _collect_for_friend(
    session: Session,
    friend: Friend,
    *,
    moment: datetime,
) -> list[InitiativeCandidate]:
    last = last_proactive_at(session, friend.id)
    if last is not None and (moment - last).total_seconds() < INITIATIVE_COOLDOWN_SECONDS:
        return []

    candidates: list[InitiativeCandidate] = []

    # ① 真实生活观察：最具体，优先。
    if getattr(friend, "life_enabled", 0):
        life = life_service.get_or_create_life(session, friend.id)
        observation = (life.pending_share or "").strip()
        last_share = _parse(life.last_share_at)
        share_cooled = (
            last_share is not None
            and (moment - last_share).total_seconds() < INITIATIVE_COOLDOWN_SECONDS
        )
        if (
            observation
            and share_text_is_worthwhile(observation)
            and not share_cooled
            and not _recently_mentioned(session, friend.id, observation)
        ):
            project_note = life_service.project_prompt_note(session, friend.id)
            hint = (
                "[系统提示] 你刚在自己的生活里做了一件事，真实的观察是：\n"
                f"{observation}\n\n"
            )
            if project_note:
                hint += f"{project_note}\n\n"
            hint += (
                "如果你想跟 TA 说，就用 1~3 个很短的气泡，每个气泡只做一件事：\n"
                "分享刚看到的东西 / 吐槽一句 / 问一个小问题 / 邀请他一起看。\n"
                "如果这是同一条生活线的后续，就自然用“我后来又看到……”接上，"
                "不要从零介绍；不要复述整段观察，不要像汇报工作；"
                "不想说就只回 `[沉默]`。"
            )
            candidates.append(
                InitiativeCandidate(
                    friend=friend,
                    kind="life",
                    content=observation,
                    hint=hint,
                    score=1.0,
                    source_id=life.current_activity_id,
                )
            )

    # ② 她攒着想说的纸条。
    thought = thought_service.next_thought(session, friend.id, now=moment)
    if thought is not None and not _recently_mentioned(session, friend.id, thought.content):
        candidates.append(
            InitiativeCandidate(
                friend=friend,
                kind="thought",
                content=thought.content,
                hint=_thought_hint(thought.content),
                score=0.9,
                source_id=thought.id,
            )
        )

    # ③ 角色状态里的 open loops。
    state = session.get(FriendState, friend.id)
    if state is not None:
        state_dict = character_state_service.state_to_dict(state)
        for item in state_dict.get("open_loops") or []:
            text = str(item.get("text") if isinstance(item, dict) else item or "").strip()
            if text and not _recently_mentioned(session, friend.id, text):
                candidates.append(
                    InitiativeCandidate(
                        friend=friend,
                        kind="open_loop",
                        content=text,
                        hint=_open_loop_hint(text),
                        score=0.55,
                    )
                )
                break

    # ④ 未解决的情节（没有 open loop 时再考虑）。
    for episode in episode_service.list_recent(session, friend.id, limit=12):
        unresolved = str(episode.unresolved or "").strip()
        if not unresolved:
            continue
        summary = str(episode.summary or "").strip()
        if summary and not _recently_mentioned(session, friend.id, summary):
            candidates.append(
                InitiativeCandidate(
                    friend=friend,
                    kind="episode",
                    content=summary,
                    hint=_episode_hint(summary, unresolved),
                    score=0.5,
                    source_id=episode.id,
                )
            )
            break

    return candidates


def collect_candidates(
    session: Session,
    *,
    now: datetime | None = None,
) -> list[InitiativeCandidate]:
    """收集所有在场朋友的**真实**开口候选，按分数从高到低返回。"""
    moment = now or datetime.now(timezone.utc)
    friends = list(
        session.execute(
            select(Friend).where(Friend.deleted_at.is_(None), Friend.presence == 1)
        ).scalars()
    )
    candidates: list[InitiativeCandidate] = []
    for friend in friends:
        candidates.extend(_collect_for_friend(session, friend, moment=moment))

    # 同一件事可能同时出现在 life / thought / open_loop 里：按分数保留最具体的一条。
    picked: list[InitiativeCandidate] = []
    for candidate in sorted(candidates, key=lambda item: item.score, reverse=True):
        if any(
            memory_service.similarity(candidate.content, old.content) >= 0.78
            for old in picked
        ):
            continue
        picked.append(candidate)
    return picked


def checkin_candidate(friend: Friend) -> InitiativeCandidate:
    """没有真实候选时，保留旧 heartbeat 的“普通冒泡”资格（仍受掷骰子控制）。"""
    return InitiativeCandidate(
        friend=friend,
        kind="checkin",
        content="",
        hint=(
            "[系统提示] 现在没人跟你说话，你想主动找 TA 说点什么。\n"
            "按你自己的性格来——可以只说一句、逗一下、问一句，或者干脆不说。"
        ),
        score=0.0,
    )


__all__ = [
    "INITIATIVE_COOLDOWN_SECONDS",
    "InitiativeCandidate",
    "checkin_candidate",
    "collect_candidates",
    "last_proactive_at",
    "share_text_is_worthwhile",
]
