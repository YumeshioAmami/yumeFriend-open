"""Live2D 模型级设置 API（D440）。"""

from __future__ import annotations

import io
import shutil
import uuid
import zipfile
from pathlib import Path

from fastapi import APIRouter, Body, Depends, File, UploadFile
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session

from backend.api.deps import get_db
from backend import config
from backend.desktop.live2d_renderer import (
    find_model_file,
    prepare_runtime_model,
    resolve_model_file,
)
from backend.services import friend_service, live2d_settings_service
from backend.services.actor import Actor
from backend.services.errors import NotFoundError

router = APIRouter(prefix="/api/live2d", tags=["live2d-models"])


def _used_by(session: Session) -> dict[str, list[dict[str, str]]]:
    result: dict[str, list[dict[str, str]]] = {}
    for friend in friend_service.list_friends(session, Actor.USER):
        if friend.visual_mode != "live2d":
            continue
        model_file = resolve_model_file(friend.id, friend.live2d_model_id)
        if model_file is None:
            continue
        model_id = live2d_settings_service.model_id_for_model_file(model_file)
        if not model_id:
            continue
        result.setdefault(model_id, []).append({"id": friend.id, "name": friend.name})
    return result


def _model_or_404(model_id: str):
    model = live2d_settings_service.model_from_id(model_id)
    if model is None:
        raise NotFoundError("LIVE2D_MODEL_NOT_FOUND", "这个 Live2D 模型不存在")
    return model


@router.get("/models")
def list_models(session: Session = Depends(get_db)) -> dict:
    used_by = _used_by(session)
    default_file = find_model_file(config.DEFAULT_LIVE2D_DIR)
    default_model_id = (
        live2d_settings_service.model_id_for_model_file(default_file)
        if default_file is not None
        else None
    )
    models = []
    for model in live2d_settings_service.all_models():
        settings = live2d_settings_service.load_settings(model)
        models.append(
            {
                "id": model.id,
                "name": str(settings.get("display_name") or model.name),
                "model_file": str(model.model_file),
                "source": model.source,
                "used_by": used_by.get(model.id, []),
                "has_settings": live2d_settings_service.has_custom_settings(model.id),
                "is_default": model.id == default_model_id,
            }
        )
    return {"models": models}


@router.post("/models/import")
async def import_model(file: UploadFile = File(...)) -> dict:
    """导入一个 Live2D 模型 zip。

    模型是目录结构（moc3 / 贴图 / exp3 / physics），所以用 zip 最稳；
    解压到 `user_data/live2d/imported/<uuid>/`，列表扫描会自动发现。
    """
    raw = await file.read()
    if not raw:
        raise ValueError("空文件")
    if len(raw) > 250 * 1024 * 1024:
        raise ValueError("模型压缩包太大了（上限 250MB）")
    if not zipfile.is_zipfile(io.BytesIO(raw)):
        raise ValueError("这不是一个 zip 压缩包")

    root = config.LIVE2D_DIR / "imported"
    root.mkdir(parents=True, exist_ok=True)
    target = root / uuid.uuid4().hex[:12]
    target.mkdir(parents=True, exist_ok=False)
    try:
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            for member in archive.infolist():
                name = member.filename.replace("\\", "/").lstrip("/")
                if not name or name.endswith("/"):
                    continue
                destination = (target / name).resolve()
                if target.resolve() not in destination.parents:
                    raise ValueError("压缩包里有非法路径")
                destination.parent.mkdir(parents=True, exist_ok=True)
                with archive.open(member) as source, destination.open("wb") as output:
                    shutil.copyfileobj(source, output)
    except Exception:
        shutil.rmtree(target, ignore_errors=True)
        raise

    models = sorted(target.rglob("*.model3.json"))
    models = [item for item in models if not item.name.startswith("_yumefriend.")]
    if not models:
        shutil.rmtree(target, ignore_errors=True)
        raise ValueError("压缩包里没有找到 *.model3.json")
    info = live2d_settings_service.model_info_for_file(models[0], source="imported")
    if info is None:
        shutil.rmtree(target, ignore_errors=True)
        raise ValueError("模型文件不完整（缺少 moc3）")
    return {
        "ok": True,
        "model": {
            "id": info.id,
            "name": info.name,
            "model_file": str(info.model_file),
        },
    }


@router.get("/models/{model_id}/meta")
def model_meta(model_id: str) -> dict:
    model = _model_or_404(model_id)
    settings = live2d_settings_service.load_settings(model)
    return {
        "model": {
            "id": model.id,
            "name": model.name,
            "model_file": str(model.model_file),
            "source": model.source,
        },
        "parameters": live2d_settings_service.model_parameter_metadata(model),
        "expressions": live2d_settings_service.expression_defs_for_model(model),
        "settings": settings,
        "model_url": f"/api/live2d/models/{model.id}/runtime.json",
    }


@router.get("/models/{model_id}/settings")
def get_settings(model_id: str) -> dict:
    model = _model_or_404(model_id)
    return {
        "model": {
            "id": model.id,
            "name": model.name,
            "model_file": str(model.model_file),
        },
        "settings": live2d_settings_service.load_settings(model),
    }


@router.put("/models/{model_id}/settings")
def put_settings(model_id: str, payload: dict = Body(...)) -> dict:
    model = _model_or_404(model_id)
    settings = live2d_settings_service.save_settings(model, payload)
    return {"ok": True, "settings": settings}


@router.get("/models/{model_id}/runtime.json")
def model_runtime_json(model_id: str) -> FileResponse:
    model = _model_or_404(model_id)
    runtime = prepare_runtime_model(model.model_file)
    return FileResponse(
        runtime,
        media_type="application/json",
        headers={"Cache-Control": "no-store, max-age=0"},
    )


@router.get("/models/{model_id}/{path:path}")
def model_file(model_id: str, path: str) -> FileResponse:
    model = _model_or_404(model_id)
    root = Path(model.model_file).parent.resolve()
    target = (root / path).resolve()
    if target != root and root not in target.parents:
        raise NotFoundError("LIVE2D_FILE_NOT_FOUND", "模型文件路径不合法")
    if not target.is_file():
        raise NotFoundError("LIVE2D_FILE_NOT_FOUND", "模型文件不存在")
    return FileResponse(target, headers={"Cache-Control": "public, max-age=3600"})
