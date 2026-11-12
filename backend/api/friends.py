"""朋友 CRUD 路由（api.md §1）+ 头像（v2，api.md【v2】3）。"""

import time
from pathlib import Path
from typing import Callable

from fastapi import APIRouter, Depends, File, Query, Response, UploadFile
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session

from backend import config as backend_config
from backend.api.deps import get_db, get_provider_factory
from backend.llm.base import ILLMProvider
from backend.schemas._compat import fields_set
from backend.schemas.friend import FriendCreate, FriendUpdate, ModeUpdate, PresenceUpdate
from backend.services import (
    avatar_description,
    avatar_service,
    background_service,
    friend_service,
    space_profile_service,
    space_service,
    thumb_service,
)
from backend.services.actor import Actor
from backend.services.errors import NotFoundError

router = APIRouter(prefix="/api/friends", tags=["friends"])
MAX_VRM_BYTES = 80 * 1024 * 1024


@router.get("")
def list_friends(
    include_deleted: bool = Query(default=False),
    session: Session = Depends(get_db),
) -> dict:
    friends = friend_service.list_friends(session, Actor.USER, include_deleted=include_deleted)
    return {"friends": [friend_service.to_dict(session, f) for f in friends]}


@router.post("", status_code=201)
def create_friend(body: FriendCreate, session: Session = Depends(get_db)) -> dict:
    friend = friend_service.create(
        session,
        Actor.USER,
        name=body.name,
        personality=body.personality,
        custom_instructions=body.custom_instructions,
        voice_examples=body.voice_examples,
        temperature=body.temperature,
        life_enabled=body.life_enabled,
        chat_pacing_enabled=body.chat_pacing_enabled,
        humanizer_enabled=body.humanizer_enabled,
        visual_mode=body.visual_mode,
        live2d_model_id=body.live2d_model_id,
        portrait_pack_id=body.portrait_pack_id,
    )
    return friend_service.to_dict(session, friend, detail=True)


@router.get("/recoverable")
def list_recoverable(session: Session = Depends(get_db)) -> dict:
    """可恢复的朋友列表（api.md §8.2 精神：软删未满 30 天的）。"""
    friends = friend_service.list_friends(session, Actor.USER, include_deleted=True)
    return {
        "recoverable": [
            {
                "id": f.id,
                "name": f.name,
                "deleted_at": f.deleted_at,
            }
            for f in friends
            if f.deleted_at is not None
        ]
    }


@router.get("/{friend_id}")
def get_friend(friend_id: str, session: Session = Depends(get_db)) -> dict:
    friend = friend_service.get(session, Actor.USER, friend_id)
    return friend_service.to_dict(session, friend, detail=True)


@router.post("/{friend_id}/space-profile")
async def build_space_profile(
    friend_id: str,
    session: Session = Depends(get_db),
    make_provider: Callable[..., ILLMProvider] = Depends(get_provider_factory),
) -> dict:
    """为小世界生成一次性的本地反应档案；失败回退默认档案。"""
    friend = friend_service.get(session, Actor.USER, friend_id)
    session.commit()  # 不把数据库事务拖过整段 LLM 调用
    profile = None
    error = ""
    try:
        provider: ILLMProvider = (
            make_provider(friend.llm_base) if friend.llm_base else make_provider()
        )
        chunks: list[str] = []
        try:
            async for event in provider.chat_stream(
                space_profile_service.prompt_messages(friend),
                max_tokens=2400,
                temperature=0.55,
            ):
                if event.get("type") == "content":
                    chunks.append(str(event.get("delta") or ""))
                elif event.get("type") == "done":
                    break
        finally:
            await provider.aclose()
        profile = space_profile_service.parse_profile("".join(chunks))
        if profile is None:
            error = "模型没有按要求返回反应档案"
    except Exception as exc:  # noqa: BLE001 —— 可选能力失败不挡住小世界
        error = str(exc)[:400]
    return {
        "ok": True,
        "source": "llm" if profile is not None else "fallback",
        "profile": profile or space_profile_service.fallback_profile(),
        "error": error,
    }


@router.patch("/{friend_id}")
def update_friend(
    friend_id: str, body: FriendUpdate, session: Session = Depends(get_db)
) -> dict:
    friend = friend_service.update(
        session,
        Actor.USER,
        friend_id,
        name=body.name,
        personality=body.personality,
        custom_instructions=body.custom_instructions,
        voice_examples=body.voice_examples,
        avatar_description=body.avatar_description,
        voice_id=body.voice_id,
        voice_reply_enabled=body.voice_reply_enabled,
        portrait_transform=body.portrait_transform,
        llm_base=body.llm_base,
        life_enabled=body.life_enabled,
        chat_pacing_enabled=body.chat_pacing_enabled,
        humanizer_enabled=body.humanizer_enabled,
        visual_mode=body.visual_mode,
        live2d_model_id=body.live2d_model_id,
        portrait_pack_id=body.portrait_pack_id,
        # v4.3（D249）：温度是三态的 —— **只有显式传了这个字段**才动它
        # （传数字 = 设成它；传 null = 恢复默认；没传 = 这次不改）
        **(
            {"temperature": body.temperature}
            if "temperature" in fields_set(body)
            else {}
        ),
    )
    return friend_service.to_dict(session, friend, detail=True)


@router.post("/{friend_id}/avatar")
async def upload_avatar(
    friend_id: str,
    file: UploadFile = File(...),
    session: Session = Depends(get_db),
) -> dict:
    """上传头像（api.md【v2】3）：服务端居中裁方 + 压到 256×256 PNG。"""
    raw = await file.read()
    friend = friend_service.get(session, Actor.USER, friend_id)
    avatar_service.save(friend, raw)
    session.flush()
    # ⚠️ 必须先提交再起后台任务：后台任务用的是**自己的新会话**，
    # 这时本请求的事务还没提交，它读到的 `avatar_path` 还是 NULL →
    # `file_path()` 拿不到文件 → 静默跳过（这个 bug 是 T2.0-9 全链路终检时抓出来的）
    session.commit()
    # v2（D84）：后台看图写"她的样子"（best-effort，不影响上传结果）
    avatar_description.spawn_describe(friend.id)
    return friend_service.to_dict(session, friend, detail=True)


@router.post("/{friend_id}/avatar/describe")
async def describe_avatar(friend_id: str, session: Session = Depends(get_db)) -> dict:
    """手动重新生成「她的样子」（D84）。

    **好赖都回 200**：没配视觉模型 / 没头像 → `ok: false` + 一句人话（D83 的口径）。
    """
    friend = friend_service.get(session, Actor.USER, friend_id)
    if not friend.avatar_path:
        return {"ok": False, "hint": "这个朋友还没有头像——先传一张再来生成"}
    ok = await avatar_description.describe_and_store(friend_id)
    if not ok:
        return {
            "ok": False,
            "hint": "没能生成（多半是没配视觉模型，或者模型没连上）——也可以自己手写一段",
        }
    session.expire_all()
    return friend_service.to_dict(session, friend_service.get(session, Actor.USER, friend_id), detail=True)


@router.delete("/{friend_id}/avatar")
def delete_avatar(friend_id: str, session: Session = Depends(get_db)) -> dict:
    """删掉头像 → 界面回到首字母色块（**不留空框**）。"""
    friend = friend_service.get(session, Actor.USER, friend_id)
    avatar_service.clear(friend)
    session.flush()
    return friend_service.to_dict(session, friend, detail=True)


@router.get("/{friend_id}/avatar")
def get_avatar(
    friend_id: str,
    w: int = Query(0, ge=0, le=1024, description="要缩到的最长边（0 = 原图）；手机列表用 128~256"),
    session: Session = Depends(get_db),
) -> FileResponse:
    """取头像图片；没有就 404（前端据此回退色块）。

    v3.1：支持 `?w=` 出压缩版（手机列表里一张头像 90~140 KB，压完 ~15 KB）+ 缓存头。
    """
    friend = friend_service.get(session, Actor.USER, friend_id)
    path = avatar_service.file_path(friend)
    if path is None:
        raise NotFoundError("AVATAR_NOT_FOUND", "这个朋友还没有头像")
    served = thumb_service.variant(path, w)
    return FileResponse(
        served,
        media_type=thumb_service.media_type_for(served),
        headers=thumb_service.cache_headers(served, max_age=86400),
    )


@router.post("/{friend_id}/avatar-model")
async def upload_avatar_model(
    friend_id: str,
    file: UploadFile = File(...),
    session: Session = Depends(get_db),
) -> dict:
    """每个朋友可选的自定义 3D 模型（目前支持 VRM）。"""
    friend_service.get(session, Actor.USER, friend_id)
    filename = str(file.filename or "")
    if not filename.lower().endswith(".vrm"):
        raise ValueError("模型只支持 .vrm 文件")
    data = await file.read(MAX_VRM_BYTES + 1)
    if len(data) > MAX_VRM_BYTES:
        raise ValueError("模型太大了（上限 80 MB）")
    if not data:
        raise ValueError("没有收到模型内容")
    backend_config.AVATAR_MODEL_DIR.mkdir(parents=True, exist_ok=True)
    (backend_config.AVATAR_MODEL_DIR / f"{friend_id}.vrm").write_bytes(data)
    model_url = f"/api/friends/{friend_id}/avatar-model?v={int(time.time() * 1000)}"
    state = space_service.update_state(
        session,
        friend_id,
        appearance={"model": model_url},
    )
    return {"ok": True, "model_url": model_url, "state": state}


@router.delete("/{friend_id}/avatar-model")
def delete_avatar_model(
    friend_id: str,
    session: Session = Depends(get_db),
) -> dict:
    friend_service.get(session, Actor.USER, friend_id)
    path = backend_config.AVATAR_MODEL_DIR / f"{friend_id}.vrm"
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass
    state = space_service.update_state(
        session,
        friend_id,
        appearance={"model": None},
    )
    return {"ok": True, "state": state}


@router.get("/{friend_id}/avatar-model")
def get_avatar_model(
    friend_id: str,
    session: Session = Depends(get_db),
) -> FileResponse:
    friend_service.get(session, Actor.USER, friend_id)
    path = backend_config.AVATAR_MODEL_DIR / f"{friend_id}.vrm"
    if not path.is_file():
        raise NotFoundError("AVATAR_MODEL_NOT_FOUND", "这个朋友还没有自定义 3D 模型")
    return FileResponse(
        path,
        media_type="model/gltf-binary",
        # URL 已带 mtime 版本；这里再要求浏览器每次校验，避免换模型后命中旧缓存。
        headers={"Cache-Control": "no-cache"},
    )


@router.post("/{friend_id}/background")
async def upload_background(
    friend_id: str,
    file: UploadFile = File(...),
    session: Session = Depends(get_db),
) -> dict:
    """上传沉浸模式的背景图（v2.2 / D107）：后端自动按 16:9 居中裁 + 压到 1920 宽。"""
    raw = await file.read()
    friend = friend_service.get(session, Actor.USER, friend_id)
    background_service.save(friend.id, raw)
    session.flush()
    return friend_service.to_dict(session, friend)


@router.delete("/{friend_id}/background")
def delete_background(friend_id: str, session: Session = Depends(get_db)) -> dict:
    """删掉背景图 → 沉浸模式回到默认氛围底。"""
    friend = friend_service.get(session, Actor.USER, friend_id)
    background_service.clear(friend.id)
    session.flush()
    return friend_service.to_dict(session, friend)


@router.get("/{friend_id}/background")
def get_background(
    friend_id: str,
    w: int = Query(0, ge=0, le=4096, description="要缩到的最长边（0 = 原图）；手机沉浸背景用 ~1280"),
    session: Session = Depends(get_db),
) -> FileResponse:
    """取背景图；没有就 404（前端据此回退默认氛围底）。"""
    friend_service.get(session, Actor.USER, friend_id)
    path = background_service.path_for(friend_id)
    if path is None:
        raise NotFoundError("BACKGROUND_NOT_FOUND", "这个朋友还没有背景图")
    # v3.1：手机端拉 1920×1080 的 JPEG 也慢 → 同样支持 `?w=` 出压缩版
    served = thumb_service.variant(path, w)
    return FileResponse(
        served,
        media_type=thumb_service.media_type_for(served),
        headers=thumb_service.cache_headers(served),
    )


@router.patch("/{friend_id}/presence")
def set_presence(
    friend_id: str, body: PresenceUpdate, session: Session = Depends(get_db)
) -> dict:
    friend = friend_service.set_presence(session, Actor.USER, friend_id, body.presence)
    return friend_service.to_dict(session, friend, detail=True)


@router.patch("/{friend_id}/mode")
def set_mode(friend_id: str, body: ModeUpdate, session: Session = Depends(get_db)) -> dict:
    friend = friend_service.set_mode(
        session, Actor.USER, friend_id, body.mode, body.role
    )
    return {
        "friend_id": friend.id,
        "current_mode": friend.current_mode,
        "current_role": friend.current_role,
    }


@router.post("/{friend_id}/read", status_code=204, response_class=Response, response_model=None)
def mark_read(friend_id: str, session: Session = Depends(get_db)) -> None:
    friend_service.mark_read(session, Actor.USER, friend_id)


@router.delete("/{friend_id}", status_code=204, response_class=Response, response_model=None)
def soft_delete(friend_id: str, session: Session = Depends(get_db)) -> None:
    friend_service.soft_delete(session, Actor.USER, friend_id)


@router.post("/{friend_id}/restore")
def restore(friend_id: str, session: Session = Depends(get_db)) -> dict:
    friend = friend_service.restore(session, Actor.USER, friend_id)
    return friend_service.to_dict(session, friend, detail=True)


@router.post("/{friend_id}/purge", status_code=204, response_class=Response, response_model=None)
def purge(friend_id: str, session: Session = Depends(get_db)) -> None:
    friend_service.purge(session, Actor.USER, friend_id)
