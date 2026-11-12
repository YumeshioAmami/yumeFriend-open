"""全局立绘包接口（v7.43 / D441）。"""

from __future__ import annotations

import json

from fastapi import APIRouter, Depends, File, Form, Response, UploadFile
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session

from backend.api.deps import get_db
from backend.services import (
    friend_service,
    portrait_pack_service,
    thumb_service,
)
from backend.services.actor import Actor
from backend.services.errors import NotFoundError, ServiceError

router = APIRouter(prefix="/api/portrait-packs", tags=["portrait-packs"])


def _used_by(session: Session) -> dict[str, list[dict[str, str]]]:
    result: dict[str, list[dict[str, str]]] = {}
    for friend in friend_service.list_friends(session, Actor.USER):
        if friend.visual_mode != "portrait":
            continue
        pack_id = portrait_pack_service.pack_id_for_friend(
            friend.id,
            friend.portrait_pack_id,
        )
        if pack_id:
            result.setdefault(pack_id, []).append({"id": friend.id, "name": friend.name})
    return result


@router.get("")
def list_packs(session: Session = Depends(get_db)) -> dict:
    friends = friend_service.list_friends(session, Actor.USER)
    packs = portrait_pack_service.list_packs({item.id: item.name for item in friends})
    used_by = _used_by(session)
    for pack in packs:
        pack["used_by"] = used_by.get(str(pack["id"]), [])
    return {"packs": packs}


@router.post("/scan")
def scan_portraits(body: dict) -> dict:
    from backend.services import portrait_service

    raw_dir = str(body.get("dir") or "")
    candidates = portrait_service.scan(raw_dir)
    used_dir = str(portrait_service.resolve_dir(raw_dir)) if raw_dir.strip() else ""
    return {"candidates": candidates, "used_dir": used_dir}


@router.post("/import")
def import_pack(body: dict) -> dict:
    name = str(body.get("name") or "").strip()
    source_dir = str(body.get("dir") or "")
    if not name:
        raise ServiceError("PORTRAIT_PACK_NAME_REQUIRED", "先给立绘包起个名字", 400)
    return portrait_pack_service.create_from_dir(name, source_dir)


@router.post("/{pack_id}/upload")
async def upload_pack_files(
    pack_id: str,
    files: list[UploadFile] = File(...),
    emotions: str = Form("[]"),
) -> dict:
    try:
        names = json.loads(emotions)
    except json.JSONDecodeError as exc:
        raise ServiceError("PORTRAIT_PACK_BAD_EMOTIONS", "emotions 不是合法 JSON", 400) from exc
    if not isinstance(names, list) or len(names) != len(files):
        raise ServiceError("PORTRAIT_PACK_BAD_EMOTIONS", "文件和情绪名数量对不上", 400)
    imported = []
    for upload, emotion in zip(files, names):
        raw = await upload.read()
        imported.append(portrait_pack_service.save_bytes(pack_id, str(emotion), raw))
    return {"imported": imported}


@router.delete("/{pack_id}", status_code=204, response_class=Response, response_model=None)
def delete_pack(pack_id: str, session: Session = Depends(get_db)) -> None:
    used_by = _used_by(session).get(pack_id) or []
    if used_by:
        names = "、".join(item["name"] for item in used_by[:4])
        raise ServiceError(
            "PORTRAIT_PACK_IN_USE",
            f"这个立绘包还在给 {names} 用，先让他们换一个",
            409,
        )
    portrait_pack_service.delete_pack(pack_id)


@router.get("/{pack_id}/{emotion}")
def get_pack_image(
    pack_id: str,
    emotion: str,
    w: int = 0,
) -> FileResponse:
    path = portrait_pack_service.file_for(pack_id, emotion)
    if path is None:
        raise NotFoundError("PORTRAIT_PACK_IMAGE_NOT_FOUND", "这个立绘包里没有这个表情")
    served = thumb_service.variant(path, w)
    return FileResponse(
        served,
        media_type=thumb_service.media_type_for(served),
        headers=thumb_service.cache_headers(served),
    )
