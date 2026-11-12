"""game_* 表的读写（P3）。只做持久化，不含游戏逻辑。"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Mapping

from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.models import GameBinding, GameEvent, GameSession, GameTask


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _new_id() -> str:
    return str(uuid.uuid4())


def _dump(value: Any) -> str | None:
    if value is None:
        return None
    return json.dumps(value, ensure_ascii=False)


def upsert_binding(
    session: Session,
    friend_id: str,
    game_kind: str,
    *,
    account_name: str | None = None,
    settings: Mapping[str, Any] | None = None,
) -> GameBinding:
    binding = session.execute(
        select(GameBinding).where(
            GameBinding.friend_id == friend_id,
            GameBinding.game_kind == game_kind,
        )
    ).scalar_one_or_none()
    now = _now()
    if binding is None:
        binding = GameBinding(
            id=_new_id(),
            friend_id=friend_id,
            game_kind=game_kind,
            account_name=account_name,
            settings=_dump(settings),
            created_at=now,
            updated_at=now,
        )
        session.add(binding)
    else:
        if account_name is not None:
            binding.account_name = account_name
        if settings is not None:
            binding.settings = _dump(settings)
        binding.updated_at = now
    session.flush()
    return binding


def create_session(
    session: Session,
    *,
    session_id: str,
    friend_id: str,
    game_kind: str,
    binding_id: str | None = None,
    config: Mapping[str, Any] | None = None,
    status: str = "running",
) -> GameSession:
    now = _now()
    row = GameSession(
        id=session_id,
        friend_id=friend_id,
        game_kind=game_kind,
        binding_id=binding_id,
        status=status,
        config=_dump(config),
        summary_state="pending",
        started_at=now,
        created_at=now,
        updated_at=now,
    )
    session.add(row)
    session.flush()
    return row


def get_session(session: Session, session_id: str) -> GameSession | None:
    return session.get(GameSession, session_id)


def finish_session(
    session: Session,
    session_id: str,
    *,
    status: str = "ended",
    error: str | None = None,
) -> GameSession | None:
    row = session.get(GameSession, session_id)
    if row is None:
        return None
    now = _now()
    row.status = status
    row.error = error
    row.ended_at = now
    row.updated_at = now
    if row.summary_state != "done":
        row.summary_state = "pending"
    session.flush()
    return row


def add_task(
    session: Session,
    *,
    session_id: str,
    adapter_task_id: str | None,
    action: str,
    args: Mapping[str, Any] | None = None,
) -> GameTask:
    task = GameTask(
        id=_new_id(),
        session_id=session_id,
        adapter_task_id=adapter_task_id,
        action=action,
        args=_dump(args or {}),
        status="running",
        created_at=_now(),
    )
    session.add(task)
    session.flush()
    return task


def update_task_by_adapter_id(
    session: Session,
    session_id: str,
    adapter_task_id: str | None,
    *,
    status: str,
    result: Any = None,
) -> GameTask | None:
    if not adapter_task_id:
        return None
    task = session.execute(
        select(GameTask).where(
            GameTask.session_id == session_id,
            GameTask.adapter_task_id == adapter_task_id,
        )
    ).scalar_one_or_none()
    if task is None:
        return None
    task.status = status
    task.result = _dump(result)
    task.finished_at = _now()
    session.flush()
    return task


def attach_adapter_task_id(
    session: Session, task_id: str, adapter_task_id: str | None
) -> GameTask | None:
    task = session.get(GameTask, task_id)
    if task is None:
        return None
    task.adapter_task_id = adapter_task_id
    session.flush()
    return task


def finish_task(
    session: Session, task_id: str, *, status: str, result: Any = None
) -> GameTask | None:
    task = session.get(GameTask, task_id)
    if task is None:
        return None
    task.status = status
    task.result = _dump(result)
    task.finished_at = _now()
    session.flush()
    return task


def update_latest_running_task(
    session: Session, session_id: str, *, status: str, result: Any = None
) -> GameTask | None:
    """事件比 task 行的 adapter_task_id 先到时的兜底：更新最近一条 running。"""
    task = session.execute(
        select(GameTask)
        .where(GameTask.session_id == session_id, GameTask.status == "running")
        .order_by(GameTask.created_at.desc())
        .limit(1)
    ).scalar_one_or_none()
    if task is None:
        return None
    task.status = status
    task.result = _dump(result)
    task.finished_at = _now()
    session.flush()
    return task


def add_event(
    session: Session,
    *,
    event_id: str,
    session_id: str,
    friend_id: str,
    event_type: str,
    payload: Mapping[str, Any] | None = None,
) -> bool:
    """写一条事件；重复 event_id 返回 False（至少一次投递去重）。"""
    if session.get(GameEvent, event_id) is not None:
        return False
    session.add(
        GameEvent(
            id=event_id,
            session_id=session_id,
            friend_id=friend_id,
            type=event_type,
            payload=_dump(payload),
            created_at=_now(),
        )
    )
    session.flush()
    return True


def recent_events(
    session: Session, session_id: str, *, limit: int = 200
) -> list[GameEvent]:
    rows = session.execute(
        select(GameEvent)
        .where(GameEvent.session_id == session_id)
        .order_by(GameEvent.created_at.desc())
        .limit(limit)
    ).scalars()
    return list(reversed(list(rows)))


def recent_summaries(
    session: Session, friend_id: str, *, days: int = 14, limit: int = 3
) -> list[GameSession]:
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    rows = session.execute(
        select(GameSession)
        .where(
            GameSession.friend_id == friend_id,
            GameSession.summary_state == "done",
            GameSession.summary.is_not(None),
            GameSession.started_at >= cutoff,
        )
        .order_by(GameSession.started_at.desc())
        .limit(limit)
    ).scalars()
    return list(rows)


def pending_summaries(session: Session, *, limit: int = 20) -> list[GameSession]:
    rows = session.execute(
        select(GameSession)
        .where(
            GameSession.summary_state == "pending",
            GameSession.status.in_(("ended", "crashed")),
        )
        .order_by(GameSession.ended_at.desc())
        .limit(limit)
    ).scalars()
    return list(rows)


def unfinished_sessions(session: Session) -> list[GameSession]:
    rows = session.execute(
        select(GameSession).where(
            GameSession.status.in_(("starting", "running", "stopping"))
        )
    ).scalars()
    return list(rows)


def mark_unfinished_crashed(session: Session) -> int:
    rows = unfinished_sessions(session)
    now = _now()
    for row in rows:
        row.status = "crashed"
        row.error = "进程重启 / 会话没正常结束"
        row.ended_at = now
        row.updated_at = now
        if row.summary_state != "done":
            row.summary_state = "pending"
    session.flush()
    return len(rows)


def event_to_dict(event: GameEvent) -> dict[str, Any]:
    return {
        "id": event.id,
        "session_id": event.session_id,
        "friend_id": event.friend_id,
        "type": event.type,
        "payload": json.loads(event.payload) if event.payload else {},
        "created_at": event.created_at,
    }


def session_to_dict(row: GameSession) -> dict[str, Any]:
    return {
        "id": row.id,
        "friend_id": row.friend_id,
        "game_kind": row.game_kind,
        "status": row.status,
        "summary": row.summary,
        "summary_state": row.summary_state,
        "error": row.error,
        "started_at": row.started_at,
        "ended_at": row.ended_at,
    }
