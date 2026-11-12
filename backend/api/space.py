"""小世界状态 / 事件 / 自主思考 API（v7.21 / D374）。"""

from __future__ import annotations

import json
import uuid
from pathlib import Path

from fastapi import APIRouter, Depends, File, Query, UploadFile
from fastapi.responses import FileResponse, StreamingResponse
from sqlalchemy.orm import Session

from backend import config as backend_config
from backend.api.deps import get_db
from backend.schemas.space import (
    SpaceActionResultIn,
    SpaceAskIn,
    SpaceEventIn,
    SpacePerceptionIn,
    SpaceReactionIn,
    SpaceStatePatch,
    SpaceThinkIn,
)
from backend.services import friend_service, space_service
from backend.services.actor import Actor

router = APIRouter(prefix="/api/friends", tags=["space"])
MAX_GLB_BYTES = 20 * 1024 * 1024


@router.get("/{friend_id}/space")
def read_space(friend_id: str, session: Session = Depends(get_db)) -> dict:
    friend_service.get(session, Actor.USER, friend_id)
    state = space_service.get_state(session, friend_id)
    # 换模型/重新烘焙 VRM 时，appearance.model 里的旧 `v=` 不会变；浏览器会按
    # 这个 URL 缓存 24 小时，导致“明明改了模型但小世界还是旧的”。这里用文件
    # mtime 重新生成版本号，文件一变 URL 就变。
    model_url = (state.get("appearance") or {}).get("model")
    if model_url:
        model_path = backend_config.AVATAR_MODEL_DIR / f"{friend_id}.vrm"
        if model_path.is_file():
            state.setdefault("appearance", {})["model"] = (
                f"/api/friends/{friend_id}/avatar-model"
                f"?v={int(model_path.stat().st_mtime)}"
            )
    return {
        "ok": True,
        "state": state,
        "events": space_service.recent_events(session, friend_id, limit=20),
        "world_model": space_service.world_snapshot(session, friend_id),
        **space_service.space_context(session, friend_id),
    }


@router.patch("/{friend_id}/space")
def patch_space(
    friend_id: str,
    body: SpaceStatePatch,
    session: Session = Depends(get_db),
) -> dict:
    friend_service.get(session, Actor.USER, friend_id)
    state = space_service.update_state(
        session,
        friend_id,
        layout=body.layout,
        mode=body.mode,
        current_action=body.current_action,
        prop_states=body.prop_states,
        world=body.world,
        appearance=body.appearance,
    )
    return {"ok": True, "state": state}


@router.get("/{friend_id}/space/events")
def list_space_events(
    friend_id: str,
    limit: int = Query(default=20, ge=1, le=100),
    session: Session = Depends(get_db),
) -> dict:
    friend_service.get(session, Actor.USER, friend_id)
    return {
        "ok": True,
        "events": space_service.recent_events(session, friend_id, limit=limit),
    }


@router.post("/{friend_id}/space/events")
def create_space_event(
    friend_id: str,
    body: SpaceEventIn,
    session: Session = Depends(get_db),
) -> dict:
    friend_service.get(session, Actor.USER, friend_id)
    event = space_service.log_event(
        session,
        friend_id,
        kind=body.kind,
        target=body.target,
        detail=body.detail,
        notable=body.notable,
    )
    return {"ok": True, "event": event}


@router.post("/{friend_id}/space/think")
async def think_space(
    friend_id: str,
    body: SpaceThinkIn,
    session: Session = Depends(get_db),
) -> dict:
    friend_service.get(session, Actor.USER, friend_id)
    result = await space_service.think(
        session,
        friend_id,
        text=body.text,
        viewer=body.viewer.model_dump() if body.viewer else None,
    )
    if body.text:
        dialogue = space_service.record_space_dialogue(
            session,
            friend_id,
            text=body.text,
            reply=str(result.get("line") or ""),
            emotion=str(result.get("emotion") or ""),
        )
        result.update(dialogue)
    else:
        action = result.get("action") or {}
        space_service.log_event(
            session,
            friend_id,
            kind="autonomy",
            target=str(action.get("target") or "") or None,
            detail={"action": action, "text": action.get("text")},
        )
    return {"ok": True, **result}


@router.post("/{friend_id}/space/chat/stream")
async def stream_space_chat(
    friend_id: str,
    body: SpaceThinkIn,
    session: Session = Depends(get_db),
) -> StreamingResponse:
    friend_service.get(session, Actor.USER, friend_id)

    async def frames():
        async for event in space_service.stream_chat(
            session,
            friend_id,
            text=body.text or "",
            viewer=body.viewer.model_dump() if body.viewer else None,
        ):
            yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"

    return StreamingResponse(
        frames(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


@router.post("/{friend_id}/space/perceive")
def perceive_space(
    friend_id: str,
    body: SpacePerceptionIn,
    session: Session = Depends(get_db),
) -> dict:
    friend_service.get(session, Actor.USER, friend_id)
    return {
        "ok": True,
        **space_service.perceive(
            session,
            friend_id,
            viewer=body.viewer.model_dump() if body.viewer else None,
            self_state=body.self_state.model_dump() if body.self_state else None,
            objects=[item.model_dump() for item in body.objects],
            paused=body.paused,
            personality_curiosity=body.personality_curiosity,
            activity_kind=body.activity_kind,
            activity_target=body.activity_target,
            activity_label=body.activity_label,
            mood=body.mood,
        ),
    }


@router.post("/{friend_id}/space/deliberate")
async def deliberate_space(
    friend_id: str,
    body: SpacePerceptionIn,
    session: Session = Depends(get_db),
) -> dict:
    friend_service.get(session, Actor.USER, friend_id)
    return {
        "ok": True,
        **await space_service.deliberate(
            session,
            friend_id,
            viewer=body.viewer.model_dump() if body.viewer else None,
            self_state=body.self_state.model_dump() if body.self_state else None,
            objects=[item.model_dump() for item in body.objects],
            personality_curiosity=body.personality_curiosity,
            activity_kind=body.activity_kind,
            activity_target=body.activity_target,
            activity_label=body.activity_label,
            mood=body.mood,
        ),
    }


@router.post("/{friend_id}/space/action-result")
def space_action_result(
    friend_id: str,
    body: SpaceActionResultIn,
    session: Session = Depends(get_db),
) -> dict:
    friend_service.get(session, Actor.USER, friend_id)
    return {
        "ok": True,
        **space_service.record_action_result(
            session,
            friend_id,
            action_id=body.action_id,
            ok=body.ok,
            detail=body.detail,
        ),
    }


@router.post("/{friend_id}/space/react")
async def react_space(
    friend_id: str,
    body: SpaceReactionIn,
    session: Session = Depends(get_db),
) -> dict:
    friend_service.get(session, Actor.USER, friend_id)
    return {
        "ok": True,
        **await space_service.react_to_touch(
            session,
            friend_id,
            zone=body.zone,
            viewer=body.viewer.model_dump() if body.viewer else None,
            self_state=body.self_state.model_dump() if body.self_state else None,
            objects=[item.model_dump() for item in body.objects],
            activity_label=body.activity_label,
            mood=body.mood,
            interaction=body.interaction,
            sustained_seconds=body.sustained_seconds,
        ),
    }


@router.post("/{friend_id}/space/ask")
async def ask_space(
    friend_id: str,
    body: SpaceAskIn,
    session: Session = Depends(get_db),
) -> dict:
    friend_service.get(session, Actor.USER, friend_id)
    return {
        "ok": True,
        **await space_service.ask_about_object(
            session,
            friend_id,
            object_key=body.object_key,
            viewer=body.viewer.model_dump() if body.viewer else None,
            activity_kind=body.activity_kind,
            activity_target=body.activity_target,
            activity_label=body.activity_label,
            mood=body.mood,
        ),
    }


@router.post("/{friend_id}/space/assets")
async def upload_space_asset(
    friend_id: str,
    file: UploadFile = File(...),
    session: Session = Depends(get_db),
) -> dict:
    """导入一个 GLB 作为小世界物件（v7.23 / D376）。"""
    friend_service.get(session, Actor.USER, friend_id)
    filename = str(file.filename or "")
    if not filename.lower().endswith(".glb"):
        raise ValueError("只支持 .glb 文件")
    data = await file.read(MAX_GLB_BYTES + 1)
    if len(data) > MAX_GLB_BYTES:
        raise ValueError("GLB 太大了（最多 20MB）")
    if not data.startswith(b"glTF"):
        raise ValueError("这不是一个有效的 GLB 文件")
    target_dir = Path(backend_config.SPACE_ASSET_DIR) / friend_id
    target_dir.mkdir(parents=True, exist_ok=True)
    stored = f"{uuid.uuid4()}.glb"
    (target_dir / stored).write_bytes(data)
    return {
        "ok": True,
        "asset": f"glb:{stored}",
        "url": f"/api/friends/{friend_id}/space/assets/{stored}",
    }


@router.get("/{friend_id}/space/assets/{filename}")
def get_space_asset(
    friend_id: str,
    filename: str,
    session: Session = Depends(get_db),
) -> FileResponse:
    friend_service.get(session, Actor.USER, friend_id)
    if Path(filename).name != filename or not filename.lower().endswith(".glb"):
        raise ValueError("文件名不合法")
    path = Path(backend_config.SPACE_ASSET_DIR) / friend_id / filename
    if not path.is_file():
        raise ValueError("这个 GLB 找不到了")
    return FileResponse(path, media_type="model/gltf-binary", filename=filename)
