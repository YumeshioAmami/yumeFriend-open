"""生活线状态与开关（v7.12 / D341）。"""

from __future__ import annotations

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy.orm import Session

from backend.api.deps import get_db
from backend.services import friend_service, life_service
from backend.services.actor import Actor

router = APIRouter(prefix="/api/life", tags=["life"])


class LifeToggle(BaseModel):
    enabled: bool


@router.get("/{friend_id}/status")
def status(friend_id: str, session: Session = Depends(get_db)) -> dict:
    friend = friend_service.get(session, Actor.USER, friend_id)
    return {"ok": True, **life_service.status_for(session, friend)}


@router.patch("/{friend_id}")
def toggle(
    friend_id: str,
    body: LifeToggle,
    session: Session = Depends(get_db),
) -> dict:
    friend = friend_service.get(session, Actor.USER, friend_id)
    life_service.set_enabled(session, friend, body.enabled)
    return {"ok": True, **life_service.status_for(session, friend)}
