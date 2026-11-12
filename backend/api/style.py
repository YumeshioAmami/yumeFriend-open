"""说话方式档案路由（D347）：查看 / 手改 / 学习记录 / 回滚。"""

from __future__ import annotations

import json

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from backend.api.deps import get_db
from backend.schemas.style import StyleChangesIn, StyleRollbackIn
from backend.services import friend_service, style_profile_service
from backend.services.actor import Actor

router = APIRouter(prefix="/api", tags=["style"])


def _current(session: Session, friend_id: str) -> dict:
    friend_service.get(session, Actor.USER, friend_id)  # 不存在 → 404
    profile = style_profile_service.get_profile(session, friend_id)
    return {
        "profile": profile,
        "version": profile["version"],
        "labels": style_profile_service.SECTION_LABELS,
        "limits": style_profile_service.SECTION_LIMITS,
    }


@router.get("/friends/{friend_id}/style")
def get_style(friend_id: str, session: Session = Depends(get_db)) -> dict:
    return _current(session, friend_id)


@router.post("/friends/{friend_id}/style/changes")
def edit_style(
    friend_id: str,
    body: StyleChangesIn,
    session: Session = Depends(get_db),
) -> dict:
    friend_service.get(session, Actor.USER, friend_id)
    changes = [
        {
            "action": item.action,
            "section": item.section,
            "lesson": item.lesson,
            "target_id": item.target_id,
            "why": item.why,
        }
        for item in body.changes
    ]
    result = style_profile_service.apply_changes(
        session,
        friend_id,
        changes,
        summary=body.summary,
        event_type="manual",
        reason="manual_edit",
    )
    return {
        "ok": True,
        "version": result["version"],
        "applied": result["applied"],
        "profile": result["profile"],
    }


@router.get("/friends/{friend_id}/style/events")
def list_style_events(
    friend_id: str,
    limit: int = 20,
    session: Session = Depends(get_db),
) -> dict:
    friend_service.get(session, Actor.USER, friend_id)
    rows = style_profile_service.list_events(session, friend_id, limit=limit)
    events = []
    for row in rows:
        try:
            changes = json.loads(row.changes_json or "[]")
        except ValueError:
            changes = []
        events.append(
            {
                "id": row.id,
                "event_type": row.event_type,
                "summary": row.summary,
                "changes": changes,
                "user_text": row.user_text,
                "assistant_excerpt": row.assistant_excerpt,
                "before_version": row.before_version,
                "after_version": row.after_version,
                "created_at": row.created_at,
            }
        )
    return {"events": events}


@router.post("/friends/{friend_id}/style/rollback")
def rollback_style(
    friend_id: str,
    body: StyleRollbackIn,
    session: Session = Depends(get_db),
) -> dict:
    friend_service.get(session, Actor.USER, friend_id)
    result = style_profile_service.rollback_event(session, friend_id, body.event_id)
    return {"ok": True, **result}


@router.delete("/friends/{friend_id}/style")
def reset_style(friend_id: str, session: Session = Depends(get_db)) -> dict:
    friend_service.get(session, Actor.USER, friend_id)
    result = style_profile_service.reset_profile(session, friend_id, reason="manual_reset")
    return {"ok": True, **result}
