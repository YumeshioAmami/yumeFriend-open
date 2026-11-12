"""日程业务逻辑（v4.0 / D243、D244）。

母表 `schedules` = **唯一真相源**。用户 / 她（工具）/ 扫描器都改这一张，但都必须走本模块：
  · 参数兜底 + 取值校验（nature / state / owner / source）；
  · 状态机（`pending → active → settled → archived`）不越权（界面 / 工具只许写 pending / settled）；
  · 每次改动写 `schedule_events` **留痕**（append-only，同 D230 的 `memory_events`）。

两条口径（D244 冻结）：
  · `date` / `time` 是**本地值**（`YYYY-MM-DD` / `HH:MM`）；"到某个日历日"不能被 UTC 挪一天。
  · `advance_minutes` = **提前量**（提醒早于时点多少分钟），**不是**"缓冲"（缓冲是提醒之后还能改多久）。
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta, timezone

from loguru import logger
from sqlalchemy import and_, or_, select
from sqlalchemy.orm import Session

from backend.models import Schedule, ScheduleEvent, Thought
from backend.models.schedule import NATURES, OWNERS, SETTLED_REASONS, SOURCES
from backend.services.actor import Actor
from backend.services.errors import NotFoundError

# 提醒之后还能改多久（从"她真的说出口"起算）；硬上限（从到点/到日）—— 过期就归档
BUFFER_DAYS = 3
HARD_LIMIT_DAYS = 7
# 每轮注入给她看的纸条上限（表无限，给她看的有限 —— D243 改版第 3 条）
INJECT_LIMIT = 3

# 界面 / 工具**只能**把状态写成这两个；active / archived 归扫描器
EXTERNAL_STATES = ("pending", "settled")

_UNSET: object = object()  # 区分"没传 time"和"传了 None（改成只到日）"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _new_id() -> str:
    return str(uuid.uuid4())


def _as_local(moment: datetime | None) -> datetime:
    """把任意时间归一成"本机时区 aware"（没给就用现在）。"""
    moment = moment or datetime.now().astimezone()
    return moment if moment.tzinfo else moment.astimezone()


def _parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


# ---------- 取值校验 ----------


def _choice(value: object, allowed: tuple[str, ...], default: str, field: str) -> str:
    text = str(value or "").strip().lower()
    if not text:
        return default
    if text not in allowed:
        raise ValueError(f"{field} 只能是 {allowed}，收到 {value!r}")
    return text


def parse_date(value: object) -> str:
    """收 `YYYY-MM-DD` / ISO8601 日期时间 → 本地日历日 `YYYY-MM-DD`。"""
    text = str(value or "").strip()
    if not text:
        raise ValueError("日程要有个日期（date，例如 2026-09-24）")
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).strftime("%Y-%m-%d")
    except ValueError as exc:
        raise ValueError(f"看不懂这个日期：{value!r}（写 2026-09-24 这种）") from exc


def parse_time(value: object) -> str | None:
    """收 `HH:MM` / `HH:MM:SS` / `H:MM` → `HH:MM`；空 = 只到日（None）。"""
    text = str(value or "").strip()
    if not text:
        return None
    parts = text.split(":")
    if len(parts) < 2 or len(parts) > 3:
        raise ValueError(f"看不懂这个时刻：{value!r}（写 15:30 这种）")
    try:
        hour, minute = int(parts[0]), int(parts[1])
    except ValueError as exc:
        raise ValueError(f"看不懂这个时刻：{value!r}（写 15:30 这种）") from exc
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise ValueError(f"时刻超出范围：{value!r}")
    return f"{hour:02d}:{minute:02d}"


def _advance(value: object) -> int:
    if value in (None, ""):
        return 0
    try:
        minutes = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"advance_minutes 得是整数（分钟），收到 {value!r}") from exc
    if minutes < 0:
        raise ValueError("advance_minutes 不能是负数")
    return minutes


# ---------- 时间计算（本地） ----------


def base_local(schedule: Schedule) -> datetime:
    """这条日程的"到点 / 到日"时刻（本机时区 aware）。只到日 = 当天 00:00。"""
    text = f"{schedule.date}T{schedule.time or '00:00'}"
    return datetime.fromisoformat(text).astimezone()


def notify_at(schedule: Schedule) -> datetime:
    """该"响 / 该进纸条"的时刻 = 到点 - 提前量。"""
    return base_local(schedule) - timedelta(minutes=schedule.advance_minutes or 0)


def hard_limit_at(schedule: Schedule) -> datetime:
    return base_local(schedule) + timedelta(days=HARD_LIMIT_DAYS)


# ---------- 留痕 ----------


def _snapshot(schedule: Schedule) -> dict:
    return {
        "title": schedule.title,
        "nature": schedule.nature,
        "date": schedule.date,
        "time": schedule.time,
        "advance_minutes": schedule.advance_minutes,
        "state": schedule.state,
        "owner": schedule.owner,
        "source": schedule.source,
        "first_said_at": schedule.first_said_at,
        "settled_at": schedule.settled_at,
        "settled_reason": schedule.settled_reason,
    }


def _record_event(
    session: Session,
    *,
    friend_id: str,
    schedule_id: str | None,
    actor: Actor | str,
    action: str,
    before: dict | None = None,
    after: dict | None = None,
) -> None:
    """留痕（best-effort）：写失败只 WARNING，绝不影响这轮对话 / 扫描。"""
    name = actor.value if isinstance(actor, Actor) else str(actor)
    try:
        session.add(
            ScheduleEvent(
                id=_new_id(),
                friend_id=friend_id,
                schedule_id=schedule_id,
                actor=name,
                action=action,
                before=json.dumps(before, ensure_ascii=False) if before else None,
                after=json.dumps(after, ensure_ascii=False) if after else None,
                created_at=_now(),
            )
        )
        session.flush()
    except Exception as exc:  # noqa: BLE001 —— 留痕失败不能带崩主流程
        logger.warning("写 schedule_events 失败（不影响这轮）：{}", exc)


def _expire_derived(session: Session, schedule: Schedule) -> int:
    """把这条日程派生出来的、还没销账的纸条作废（改期 / 删 / 归档时用）。"""
    rows = session.execute(
        select(Thought).where(
            Thought.schedule_id == schedule.id,
            Thought.state.in_(("pending", "awaiting")),
        )
    ).scalars()
    count = 0
    for thought in rows:
        thought.state = "expired"
        thought.updated_at = _now()
        count += 1
    if count:
        session.flush()
    return count


# ---------- 增删改查 ----------


def create(
    session: Session,
    actor: Actor,
    friend_id: str,
    *,
    title: str,
    date: object,
    nature: object = "remind",
    time: object = None,
    advance_minutes: object = 0,
    owner: object = "user",
    source: object = "chat",
) -> Schedule:
    text = (title or "").strip()
    if not text:
        raise ValueError("日程得有件事（title 不能为空）")
    schedule = Schedule(
        id=_new_id(),
        friend_id=friend_id,
        title=text,
        nature=_choice(nature, NATURES, "remind", "nature"),
        date=parse_date(date),
        time=parse_time(time),
        advance_minutes=_advance(advance_minutes),
        state="pending",
        owner=_choice(owner, OWNERS, "user", "owner"),
        source=_choice(source, SOURCES, "chat", "source"),
        created_at=_now(),
    )
    session.add(schedule)
    session.flush()
    _record_event(
        session,
        friend_id=friend_id,
        schedule_id=schedule.id,
        actor=actor,
        action="create",
        after=_snapshot(schedule),
    )
    return schedule


def _require(session: Session, schedule_id: str) -> Schedule:
    schedule = session.get(Schedule, schedule_id)
    if schedule is None or schedule.deleted_at is not None:
        raise NotFoundError("SCHEDULE_NOT_FOUND", "找不到这条日程")
    return schedule


def update(
    session: Session,
    actor: Actor,
    schedule_id: str,
    *,
    title: object = None,
    nature: object = None,
    date: object = None,
    time: object = _UNSET,
    advance_minutes: object = None,
    owner: object = None,
    state: object = None,
    settled_reason: object = None,
) -> Schedule:
    """改一条日程（改期 / 改内容 / 标完成）。

    `state` 只允许 `pending`（重开 / 改期）与 `settled`（人说做了 / 不做了）；
    **传了新的 date/time 且真的变了 → 自动回 `pending`**（派生纸条重置），这就是"改期"。
    """
    schedule = _require(session, schedule_id)
    before: dict = {}
    after: dict = {}

    if title is not None:
        text = str(title).strip()
        if not text:
            raise ValueError("title 不能改成空的")
        if text != schedule.title:
            before["title"], after["title"] = schedule.title, text
            schedule.title = text
    if nature is not None:
        value = _choice(nature, NATURES, schedule.nature, "nature")
        if value != schedule.nature:
            before["nature"], after["nature"] = schedule.nature, value
            schedule.nature = value
    if owner is not None:
        value = _choice(owner, OWNERS, schedule.owner, "owner")
        if value != schedule.owner:
            before["owner"], after["owner"] = schedule.owner, value
            schedule.owner = value

    rescheduled = False
    if date is not None:
        value = parse_date(date)
        if value != schedule.date:
            before["date"], after["date"] = schedule.date, value
            schedule.date = value
            rescheduled = True
    if time is not _UNSET:
        value = parse_time(time)
        if value != schedule.time:
            before["time"], after["time"] = schedule.time, value
            schedule.time = value
            rescheduled = True
    if advance_minutes is not None:
        value = _advance(advance_minutes)
        if value != schedule.advance_minutes:
            before["advance_minutes"], after["advance_minutes"] = (
                schedule.advance_minutes,
                value,
            )
            schedule.advance_minutes = value

    if state is not None:
        target = _choice(state, EXTERNAL_STATES, "pending", "state")
        if target == "settled":
            reason = _choice(settled_reason, SETTLED_REASONS, "done", "settled_reason")
            if schedule.state != "settled" or schedule.settled_reason != reason:
                before.setdefault("state", schedule.state)
                after["state"] = "settled"
                schedule.state = "settled"
                schedule.settled_at = _now()
                schedule.settled_reason = reason
                schedule.updated_at = _now()
                _expire_derived(session, schedule)
                session.flush()
                _record_event(
                    session,
                    friend_id=schedule.friend_id,
                    schedule_id=schedule.id,
                    actor=actor,
                    action="settle",
                    before=before,
                    after=after,
                )
                return schedule
        else:  # pending = 重开 / 改期
            if schedule.state != "pending":
                before.setdefault("state", schedule.state)
                after["state"] = "pending"
                schedule.state = "pending"
                schedule.first_said_at = None
                schedule.settled_at = None
                schedule.settled_reason = None
                _expire_derived(session, schedule)
                rescheduled = True

    if rescheduled and schedule.state != "pending":
        before.setdefault("state", schedule.state)
        schedule.state = "pending"
        schedule.first_said_at = None
        schedule.settled_at = None
        schedule.settled_reason = None
        after["state"] = "pending"
        _expire_derived(session, schedule)

    schedule.updated_at = _now()
    session.flush()
    if before:
        action = "reschedule" if rescheduled else "update"
        _record_event(
            session,
            friend_id=schedule.friend_id,
            schedule_id=schedule.id,
            actor=actor,
            action=action,
            before=before,
            after=after,
        )
    return schedule


def settle(session: Session, actor: Actor, schedule_id: str, *, reason: str = "done") -> Schedule:
    """人说"做了 / 不做了" → settled（缓冲到期后扫描器再归档）。"""
    return update(session, actor, schedule_id, state="settled", settled_reason=reason)


def remove(session: Session, actor: Actor, schedule_id: str) -> None:
    """软删（30 天后物理清理）；派生纸条一并作废。"""
    schedule = _require(session, schedule_id)
    schedule.deleted_at = _now()
    schedule.updated_at = schedule.deleted_at
    _expire_derived(session, schedule)
    session.flush()
    _record_event(
        session,
        friend_id=schedule.friend_id,
        schedule_id=schedule.id,
        actor=actor,
        action="delete",
        before={"title": schedule.title, "deleted": False},
        after={"deleted": True},
    )


def list_for(
    session: Session,
    *,
    friend_id: str | None = None,
    state: str | None = None,
    owner: str | None = None,
    include_archived: bool = False,
) -> list[Schedule]:
    conditions = [Schedule.deleted_at.is_(None)]
    if friend_id:
        conditions.append(Schedule.friend_id == friend_id)
    if state:
        if state not in ("pending", "active", "settled", "archived"):
            raise ValueError(f"state 只能是 pending/active/settled/archived，收到 {state!r}")
        conditions.append(Schedule.state == state)
    elif not include_archived:
        conditions.append(Schedule.state != "archived")
    if owner:
        conditions.append(Schedule.owner == _choice(owner, OWNERS, "user", "owner"))
    stmt = (
        select(Schedule)
        .where(*conditions)
        .order_by(Schedule.date, Schedule.time, Schedule.created_at)
    )
    return list(session.execute(stmt).scalars())


# ---------- 扫描器用的推进（只有扫描器能调这些） ----------


def scan_due(session: Session, *, now: datetime | None = None) -> list[Schedule]:
    """`pending` 且已到点 / 到日（按通知时刻算，含提前量）。"""
    moment = _as_local(now)
    rows = session.execute(
        select(Schedule)
        .where(Schedule.state == "pending", Schedule.deleted_at.is_(None))
        .order_by(Schedule.date, Schedule.time)
    ).scalars()
    return [s for s in rows if notify_at(s) <= moment]


def activate(
    session: Session, schedule: Schedule, *, now: datetime | None = None, said: bool = False
) -> Schedule:
    """`pending → active`（扫描器）。`said=True` = 她这一刻真的把它说出口了。"""
    if schedule.state != "active":
        schedule.state = "active"
    if said and not schedule.first_said_at:
        schedule.first_said_at = (now or datetime.now(timezone.utc)).isoformat()
    schedule.updated_at = _now()
    session.flush()
    _record_event(
        session,
        friend_id=schedule.friend_id,
        schedule_id=schedule.id,
        actor="system",
        action="activate",
        after={"state": "active", "first_said_at": schedule.first_said_at},
    )
    return schedule


def mark_said(session: Session, schedule: Schedule, *, now: datetime | None = None) -> None:
    """她真的把这件事说出口了 → 记 `first_said_at`（缓冲从这一刻起算）。"""
    if schedule.first_said_at:
        return
    schedule.first_said_at = (now or datetime.now(timezone.utc)).isoformat()
    schedule.updated_at = _now()
    session.flush()


def archive_expired(session: Session, *, now: datetime | None = None) -> int:
    """过了缓冲 / 硬上限的 → `archived`（幂等）。

    · 硬上限：到点/到日 + `HARD_LIMIT_DAYS` 天（pending / active / settled 都算）；
    · 缓冲：`settled` 从 `settled_at` 起、`active` 从 `first_said_at` 起 + `BUFFER_DAYS` 天。
    """
    moment = _as_local(now)
    rows = session.execute(
        select(Schedule).where(
            Schedule.state.in_(("pending", "active", "settled")),
            Schedule.deleted_at.is_(None),
        )
    ).scalars()
    count = 0
    for schedule in rows:
        reason = None
        if hard_limit_at(schedule) <= moment:
            reason = "hard_limit"
        elif schedule.state == "settled":
            settled_at = _parse_iso(schedule.settled_at)
            if settled_at and settled_at + timedelta(days=BUFFER_DAYS) <= moment:
                reason = "buffer"
        elif schedule.state == "active":
            said_at = _parse_iso(schedule.first_said_at)
            if said_at and said_at + timedelta(days=BUFFER_DAYS) <= moment:
                reason = "buffer"
        if reason is None:
            continue
        schedule.state = "archived"
        schedule.updated_at = _now()
        _expire_derived(session, schedule)
        count += 1
        _record_event(
            session,
            friend_id=schedule.friend_id,
            schedule_id=schedule.id,
            actor="system",
            action="archive",
            after={"state": "archived", "reason": reason},
        )
    if count:
        session.flush()
    return count


def purge_deleted(session: Session, *, older_than_days: int = 30) -> int:
    """物理清掉"软删 / 已归档超过 N 天"的日程（跟朋友软删期同一个数，D138）。

    归档的（archived）不会再被扫到，留着只会让表一直涨 —— 跟老 reminders 的
    "已完成 30 天清掉"同一个口径。
    """
    from backend.services import sync_service  # noqa: PLC0415

    cutoff = (datetime.now(timezone.utc) - timedelta(days=older_than_days)).isoformat()
    rows = list(
        session.execute(
            select(Schedule).where(
                or_(
                    and_(Schedule.deleted_at.is_not(None), Schedule.deleted_at < cutoff),
                    and_(Schedule.state == "archived", Schedule.updated_at < cutoff),
                )
            )
        ).scalars()
    )
    for schedule in rows:
        sync_service.tombstone(session, "schedules", schedule.id)
        session.delete(schedule)
    session.flush()
    return len(rows)


def to_dict(schedule: Schedule) -> dict:
    return {
        "id": schedule.id,
        "friend_id": schedule.friend_id,
        "title": schedule.title,
        "nature": schedule.nature,
        "date": schedule.date,
        "time": schedule.time,
        "advance_minutes": schedule.advance_minutes,
        "state": schedule.state,
        "owner": schedule.owner,
        "source": schedule.source,
        "first_said_at": schedule.first_said_at,
        "settled_at": schedule.settled_at,
        "settled_reason": schedule.settled_reason,
        "created_at": schedule.created_at,
        "updated_at": schedule.updated_at,
    }


def events_for(session: Session, friend_id: str, *, limit: int = 50) -> list[dict]:
    """某朋友日程的改动留痕（倒序）。"""
    rows = session.execute(
        select(ScheduleEvent)
        .where(ScheduleEvent.friend_id == friend_id)
        .order_by(ScheduleEvent.created_at.desc())
        .limit(max(1, min(200, limit)))
    ).scalars()
    return [
        {
            "id": event.id,
            "schedule_id": event.schedule_id,
            "actor": event.actor,
            "action": event.action,
            "before": event.before,
            "after": event.after,
            "created_at": event.created_at,
        }
        for event in rows
    ]


def upcoming_for_prompt(
    session: Session, friend_id: str, *, limit: int = 10
) -> str | None:
    """v4.1（D246）：把她**当前名下的日程**（pending + active）列成给模型看的一小段。

    为什么要它：光靠 `list_schedules` 她**想不起来**去用（实测她一次都没调过）——
    把清单直接摆进 system prompt，她才"记得"自己记了这些事，也才可能在你说"办完了"时真的去结算。
    表无限、给她看的有限：只列最近的 `limit` 条。
    """
    rows = [
        s
        for s in list_for(session, friend_id=friend_id, include_archived=False)
        if s.state in ("pending", "active")
    ][:limit]
    if not rows:
        return None
    lines: list[str] = []
    for s in rows:
        when = s.date + (f" {s.time}" if s.time else "（只到日）")
        who = "我自己的事" if s.owner == "friend" else "他的事"
        state = "待响" if s.state == "pending" else "已提过"
        lines.append(f"- id={s.id} ｜ {when} ｜ {s.title} ｜ {who} ｜ {state}")
    return "\n".join(lines)
