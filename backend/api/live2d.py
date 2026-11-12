"""沉浸模式 Live2D 帧接口（v7.30 / D428）。

返回的是透明 WebP 单帧。前端用节流轮询（约 10fps）把帧画到 `<img>`，
情绪、口型和程序化呼吸/摆动都由桌面端同一个 `Live2DRenderer` 生成。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session

from backend.api.deps import get_db
from backend.desktop.live2d_renderer import prepare_runtime_model, resolve_model_file
from backend.services import friend_service
from backend.services import live2d_settings_service
from backend.services.actor import Actor
from backend.services.errors import NotFoundError
from backend.services.live2d_web_service import Live2DWebService

router = APIRouter(prefix="/api/friends", tags=["live2d"])


@router.get("/{friend_id}/live2d/meta")
def live2d_meta(friend_id: str, session: Session = Depends(get_db)) -> dict:
    friend = friend_service.get(session, Actor.USER, friend_id)
    model = resolve_model_file(friend_id, friend.live2d_model_id)
    model_info = (
        live2d_settings_service.model_info_for_file(model) if model is not None else None
    )
    settings = (
        live2d_settings_service.load_settings(model_info) if model_info is not None else None
    )
    return {
        "available": model is not None,
        "model": str(model.name) if model is not None else None,
        "model_id": model_info.id if model_info is not None else None,
        "model_url": (
            f"/api/friends/{friend_id}/live2d/model/runtime.json"
            if model is not None
            else None
        ),
        "labels": (
            [
                {"id": item.get("id"), "name": item.get("name"), "aliases": item.get("aliases", [])}
                for item in (settings or {}).get("labels", [])
            ]
            if settings
            else []
        ),
        "settings": settings,
        "expressions": (
            live2d_settings_service.expression_defs_for_model(model_info)
            if model_info is not None
            else []
        ),
    }


@router.get("/{friend_id}/live2d/model/runtime.json")
def live2d_runtime_json(friend_id: str, session: Session = Depends(get_db)) -> FileResponse:
    """浏览器端 Cubism 加载器的入口 JSON；相对路径会落在 model/ 目录下。"""
    friend = friend_service.get(session, Actor.USER, friend_id)
    model = resolve_model_file(friend_id, friend.live2d_model_id)
    if model is None:
        raise NotFoundError("LIVE2D_NOT_FOUND", "这个朋友还没有 Live2D 模型")
    runtime = prepare_runtime_model(model)
    return FileResponse(
        runtime,
        media_type="application/json",
        headers={"Cache-Control": "no-store, max-age=0"},
    )


@router.get("/{friend_id}/live2d/model/{path:path}")
def live2d_model_file(
    friend_id: str,
    path: str,
    session: Session = Depends(get_db),
) -> FileResponse:
    """按模型目录服务 moc3 / 贴图 / exp3 / motion3。"""
    friend = friend_service.get(session, Actor.USER, friend_id)
    model = resolve_model_file(friend_id, friend.live2d_model_id)
    if model is None:
        raise NotFoundError("LIVE2D_NOT_FOUND", "这个朋友还没有 Live2D 模型")
    root = model.parent.resolve()
    target = (root / path).resolve()
    if target != root and root not in target.parents:
        raise NotFoundError("LIVE2D_FILE_NOT_FOUND", "模型文件路径不合法")
    if not target.is_file():
        raise NotFoundError("LIVE2D_FILE_NOT_FOUND", "模型文件不存在")
    return FileResponse(target, headers={"Cache-Control": "public, max-age=3600"})


@router.get("/{friend_id}/live2d/frame")
def live2d_frame(
    friend_id: str,
    mood: str = Query("", description="情绪标签原文"),
    speaking: bool = Query(False, description="是否正在说话（口型）"),
    w: int = Query(420, ge=64, le=1024),
    h: int = Query(760, ge=64, le=1536),
    session: Session = Depends(get_db),
) -> Response:
    friend = friend_service.get(session, Actor.USER, friend_id)
    if resolve_model_file(friend_id, friend.live2d_model_id) is None:
        raise NotFoundError("LIVE2D_NOT_FOUND", "这个朋友还没有 Live2D 模型")
    try:
        data = Live2DWebService.shared().render(
            friend_id=friend_id,
            model_id=friend.live2d_model_id,
            mood=mood,
            speaking=speaking,
            width=w,
            height=h,
        )
    except TimeoutError as exc:
        raise HTTPException(status_code=503, detail="Live2D 这一帧没来得及画出来") from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return Response(
        content=data,
        media_type="image/webp",
        headers={"Cache-Control": "no-store, max-age=0"},
    )
