"""功能中心 API（v7.20 / D373）。"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel
from sqlalchemy.orm import Session

from backend.api.deps import get_db
from backend.features import catalog, events, flags
from backend.scheduler.jobs import sync_scheduler
from backend.services import settings_service
from backend.services.errors import ServiceError

router = APIRouter(prefix="/api/features", tags=["features"])


class FeatureToggle(BaseModel):
    enabled: bool


@router.get("")
def list_features(session: Session = Depends(get_db)) -> dict:
    enabled = flags.snapshot()
    if enabled is None:
        enabled = flags.refresh(session)
    return {"ok": True, "features": flags.public_features(enabled)}


@router.patch("/{feature_id}")
def toggle_feature(
    feature_id: str,
    body: FeatureToggle,
    request: Request,
    session: Session = Depends(get_db),
) -> dict:
    feature = catalog.get_feature(feature_id)
    if feature is None:
        raise ServiceError("FEATURE_NOT_FOUND", f"没有这个功能：{feature_id}", 404)
    if feature.core:
        raise ServiceError("FEATURE_CORE", "核心聊天功能不能关闭。", 409)

    row = settings_service.set_feature(session, feature_id, body.enabled)
    enabled = flags.enabled_ids_from_row(row)
    flags.set_cache(enabled)

    scheduler = getattr(request.app.state, "scheduler", None)
    if scheduler is not None:
        sync_scheduler(scheduler, enabled)

    events.emit(
        "feature.toggled",
        feature_id=feature_id,
        enabled=bool(body.enabled),
        name=feature.name,
    )
    public = next(
        item for item in flags.public_features(enabled) if item["id"] == feature_id
    )
    return {"ok": True, "feature": public}
