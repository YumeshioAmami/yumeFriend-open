"""日程路由（api.md【v4.0】/ D243、D244）。

母表唯一：界面 / 她（工具）/ 扫描器改的都是这一张。界面**不许**直接写 active / archived。
"""

from fastapi import APIRouter, Depends, Query, Response
from sqlalchemy.orm import Session

from backend.api.deps import get_db
from backend.schemas._compat import model_dump
from backend.schemas.schedule import ScheduleCreate, ScheduleUpdate
from backend.services import friend_service, schedule_service
from backend.services.actor import Actor

router = APIRouter(prefix="/api", tags=["schedules"])


@router.get("/schedules")
def list_schedules(
    friend_id: str | None = Query(default=None),
    state: str | None = Query(default=None),
    owner: str | None = Query(default=None),
    include_archived: bool = Query(default=False),
    session: Session = Depends(get_db),
) -> dict:
    rows = schedule_service.list_for(
        session,
        friend_id=friend_id,
        state=state,
        owner=owner,
        include_archived=include_archived,
    )
    return {"schedules": [schedule_service.to_dict(s) for s in rows]}


@router.post("/schedules", status_code=201)
def create_schedule(body: ScheduleCreate, session: Session = Depends(get_db)) -> dict:
    friend_service.get(session, Actor.USER, body.friend_id)  # 朋友不存在 → 404
    schedule = schedule_service.create(
        session,
        Actor.USER,
        body.friend_id,
        title=body.title,
        date=body.date,
        time=body.time,
        nature=body.nature or "remind",
        advance_minutes=body.advance_minutes or 0,
        owner=body.owner or "user",
        source="ui",
    )
    return schedule_service.to_dict(schedule)


@router.patch("/schedules/{schedule_id}")
def update_schedule(
    schedule_id: str, body: ScheduleUpdate, session: Session = Depends(get_db)
) -> dict:
    """改期 / 改内容 / 标完成。只有显式传了的字段才会动（`exclude_unset`）。"""
    fields = model_dump(body, exclude_unset=True)
    schedule = schedule_service.update(session, Actor.USER, schedule_id, **fields)
    return schedule_service.to_dict(schedule)


@router.delete(
    "/schedules/{schedule_id}",
    status_code=204,
    response_class=Response,
    response_model=None,
)
def delete_schedule(schedule_id: str, session: Session = Depends(get_db)) -> Response:
    schedule_service.remove(session, Actor.USER, schedule_id)
    return Response(status_code=204)


@router.get("/friends/{friend_id}/schedule-events")
def list_schedule_events(
    friend_id: str,
    limit: int = Query(default=50, ge=1, le=200),
    session: Session = Depends(get_db),
) -> dict:
    """她动过什么日程的留痕（同 api.md §3.4 记忆改动的思路）。"""
    friend_service.get(session, Actor.USER, friend_id)
    return {"events": schedule_service.events_for(session, friend_id, limit=limit)}
