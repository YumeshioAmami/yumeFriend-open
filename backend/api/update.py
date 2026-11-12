"""桌面自动更新 API（D456）。"""

from fastapi import APIRouter

from backend.services import update_service

router = APIRouter(prefix="/api/update", tags=["update"])


@router.get("/status")
def status(force: bool = False) -> dict:
    return update_service.check(force=force)


@router.post("/download")
def download() -> dict:
    return update_service.start_download()


@router.post("/apply")
def apply() -> dict:
    return update_service.apply_update()


@router.get("/history")
def history() -> dict:
    return {"items": update_service.history()}
