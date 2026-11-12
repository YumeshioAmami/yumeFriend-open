"""LLM 真实用量统计（D331）。"""

from fastapi import APIRouter, Query

from backend.services import usage_service

router = APIRouter(prefix="/api/usage", tags=["usage"])


@router.get("/summary")
def usage_summary(
    days: int = Query(default=1, ge=1, le=90),
    recent_limit: int = Query(default=20, ge=0, le=200),
) -> dict:
    return usage_service.summary(days=days, recent_limit=recent_limit)
