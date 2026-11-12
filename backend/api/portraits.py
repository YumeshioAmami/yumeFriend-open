"""立绘接口（v2.2 / T2.2-3；api.md 的【v2.2】1）。

情绪 = 文件名（对齐 LingChat，D102）。所有失败给人话（D83）。
"""

from __future__ import annotations

import json

from fastapi import APIRouter, Depends, File, Form, Query, Response, UploadFile
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session

from backend.api.deps import get_db
from backend.services import friend_service, portrait_pack_service, portrait_service, thumb_service
from backend.services.actor import Actor
from backend.services.errors import NotFoundError

router = APIRouter(prefix="/api/friends", tags=["portraits"])


def _pack_id(friend) -> str | None:
    return portrait_pack_service.pack_id_for_friend(friend.id, friend.portrait_pack_id)


@router.get("/{friend_id}/portraits")
def list_portraits(friend_id: str, session: Session = Depends(get_db)) -> dict:
    friend = friend_service.get(session, Actor.USER, friend_id)
    return {"portraits": portrait_service.list_portraits(friend_id, _pack_id(friend))}


@router.get("/{friend_id}/portraits/{emotion}")
def get_portrait(
    friend_id: str,
    emotion: str,
    w: int = Query(0, ge=0, le=4096, description="要缩到的最长边（0 = 原图）；手机沉浸模式用 ~760"),
    session: Session = Depends(get_db),
) -> FileResponse:
    friend = friend_service.get(session, Actor.USER, friend_id)
    path = portrait_service.file_for(friend_id, emotion, _pack_id(friend))
    if path is None:
        raise NotFoundError("PORTRAIT_NOT_FOUND", "没有这个表情的立绘")
    # v3.1：手机端拉立绘慢（一张 ~288 KB，换情绪就拉一张）→ 支持 `?w=` 压缩版 + 缓存住
    served = thumb_service.variant(path, w)
    return FileResponse(
        served,
        media_type=thumb_service.media_type_for(served),
        headers=thumb_service.cache_headers(served),
    )


@router.post("/{friend_id}/portraits/scan")
def scan_portraits(friend_id: str, body: dict, session: Session = Depends(get_db)) -> dict:
    friend = friend_service.get(session, Actor.USER, friend_id)
    raw_dir = str(body.get("dir") or "")
    candidates = portrait_service.scan(raw_dir)
    # 用户填的是"上一层"时，我们把真正扫的那一层回给前端，界面好说清楚（D108）
    used_dir = str(portrait_service.resolve_dir(raw_dir)) if raw_dir.strip() else ""
    return {"candidates": candidates, "used_dir": used_dir}


@router.post("/{friend_id}/portraits/import")
def import_portraits(friend_id: str, body: dict, session: Session = Depends(get_db)) -> dict:
    friend = friend_service.get(session, Actor.USER, friend_id)
    emotions = body.get("emotions")
    result = portrait_service.import_from(
        session,
        friend,
        source_dir=str(body.get("dir") or ""),
        emotions=[str(item) for item in emotions] if isinstance(emotions, list) else None,
        use_avatar=bool(body.get("use_avatar")),
    )
    session.commit()  # 先提交再让前端看（D94）
    return result


@router.post("/{friend_id}/portraits/batch")
async def upload_portraits_batch(
    friend_id: str,
    files: list[UploadFile] = File(...),
    emotions: str = Form("[]"),
    session: Session = Depends(get_db),
) -> dict:
    """v7.0（D305）：手机/平板多选立绘；emotions 是 JSON 字符串数组。"""
    friend = friend_service.get(session, Actor.USER, friend_id)
    try:
        names = json.loads(emotions)
    except json.JSONDecodeError as exc:
        raise ValueError("emotions 不是合法 JSON") from exc
    if not isinstance(names, list) or len(names) != len(files):
        raise ValueError("文件和情绪名数量对不上")
    imported: list[dict] = []
    for upload, emotion in zip(files, names):
        raw = await upload.read()
        imported.append(
            portrait_service.save_bytes(friend_id, str(emotion), raw, _pack_id(friend))
        )
    session.commit()
    return {"imported": imported}


@router.delete(
    "/{friend_id}/portraits/{emotion}",
    status_code=204,
    response_class=Response,
    response_model=None,
)
def delete_portrait(friend_id: str, emotion: str, session: Session = Depends(get_db)) -> None:
    friend = friend_service.get(session, Actor.USER, friend_id)
    portrait_service.delete(friend_id, emotion, _pack_id(friend))
