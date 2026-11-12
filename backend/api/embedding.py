"""向量检索的状态与重建（D231；给设置页的「向量检索」卡片用）。

口径：
  · 状态接口**不加载模型**（加载要 ~1 秒、首次还要下载）——只报"现在是不是已经加载了"；
  · 想让它真的可用，用户点「测试」或「重建索引」，那时才在**线程**里加载。
"""

from __future__ import annotations

import asyncio
import shutil
import uuid
from pathlib import Path, PurePosixPath

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from backend import config
from backend.api.deps import get_db
from backend.models import Memory, MemoryEmbedding
from backend.services import embedding_download_service, embedding_service

router = APIRouter(prefix="/api/embedding", tags=["embedding"])

MAX_REBUILD_ROUNDS = 40  # 一轮 64 条 → 一次最多补 ~2560 条，够个人用


def _cache_mb() -> float:
    try:
        total = sum(
            path.stat().st_size
            for path in config.MODEL_CACHE_DIR.rglob("*")
            if path.is_file()
        )
    except OSError:
        return 0.0
    return round(total / 1024 / 1024, 1)


def _counts(session: Session, storage_model: str) -> tuple[int, int]:
    total = session.execute(
        select(func.count()).select_from(Memory).where(Memory.deleted_at.is_(None))
    ).scalar_one()
    indexed = session.execute(
        select(func.count())
        .select_from(MemoryEmbedding)
        .join(Memory, Memory.id == MemoryEmbedding.memory_id)
        .where(
            Memory.deleted_at.is_(None),
            MemoryEmbedding.model == storage_model,
        )
    ).scalar_one()
    return int(indexed), int(total)


def _status(session: Session) -> dict:
    cfg = embedding_service.config_from_session(session)
    indexed, total = _counts(session, cfg.storage_model)
    download = embedding_download_service.status()
    if cfg.provider == "openai":
        configured = bool(cfg.base_url and cfg.model)
    elif cfg.provider == "local_file":
        configured = embedding_service.local_file_ready(cfg.model_path)
    elif cfg.model == config.EMBEDDING_DEFAULT_MODEL:
        configured = bool(download["installed"])
    else:
        configured = embedding_download_service.installed_for(
            cfg.model, cfg.local_cache_dir
        )
    return {
        "enabled": cfg.enabled,
        "configured": configured,
        "provider": cfg.provider,
        "protocol": cfg.protocol,
        "model": cfg.model,
        "dim": cfg.dim,
        "ready": embedding_service.ready(cfg),
        "cache_dir": str(config.MODEL_CACHE_DIR),
        "cache_mb": _cache_mb(),
        "indexed": indexed,
        "total": total,
        "min_similarity": cfg.min_similarity,
        "installed": download["installed"],
        "download": download,
    }


@router.get("/model")
def model_status() -> dict:
    """普通设置里那张「下载增强模型」卡片的状态。"""
    return embedding_download_service.status()


@router.post("/model/download")
def download_model() -> dict:
    """开始从资源页下载推荐模型（后台线程；前端轮询 /model 看进度）。"""
    return embedding_download_service.start_download()


@router.post("/local-model/upload")
async def upload_local_model(files: list[UploadFile] = File(...)) -> dict:
    """接收前端选中的本地模型文件夹（model.onnx + tokenizer.json）。"""
    root = config.MODEL_CACHE_DIR / "local_models" / uuid.uuid4().hex
    root.mkdir(parents=True, exist_ok=True)
    saved: list[str] = []
    try:
        for upload in files:
            raw_name = (upload.filename or "").replace("\\", "/")
            parts = [
                part
                for part in PurePosixPath(raw_name).parts
                if part not in {"", ".", ".."} and ":" not in part
            ]
            if not parts:
                continue
            target = root.joinpath(*parts)
            target.parent.mkdir(parents=True, exist_ok=True)
            with target.open("wb") as handle:
                shutil.copyfileobj(upload.file, handle)
            saved.append(str(target.relative_to(root)))
        has_onnx = any(name.lower().endswith(".onnx") for name in saved)
        has_tokenizer = any(Path(name).name == "tokenizer.json" for name in saved)
        if not has_onnx or not has_tokenizer:
            raise HTTPException(
                status_code=400,
                detail="模型文件夹里至少要包含一个 .onnx 和 tokenizer.json",
            )
    except HTTPException:
        shutil.rmtree(root, ignore_errors=True)
        raise
    except Exception as exc:  # noqa: BLE001
        shutil.rmtree(root, ignore_errors=True)
        raise HTTPException(status_code=400, detail=f"保存本地模型失败：{exc}") from exc
    return {"ok": True, "path": str(root), "files": saved}


@router.get("/status")
def status(session: Session = Depends(get_db)) -> dict:
    """向量检索现在什么状态（不加载模型）。"""
    return _status(session)


def _rebuild_all() -> int:
    """把索引补齐（自己开 session —— 这个函数跑在线程里）。"""
    from backend.db import session_scope  # noqa: PLC0415

    added = 0
    with session_scope() as session:
        for _ in range(MAX_REBUILD_ROUNDS):
            count = embedding_service.backfill(session)
            added += count
            if count == 0:
                break
        session.commit()
    return added


@router.post("/backfill")
async def backfill(session: Session = Depends(get_db)) -> dict:
    """手动重建索引（设置页按钮）。模型会在这一步被加载（线程里，不堵别的请求）。"""
    cfg = embedding_service.config_from_session(session)
    if not cfg.enabled:
        return {"ok": False, "error": "向量检索在设置里是关的", "added": 0, **_status(session)}
    added = await asyncio.to_thread(_rebuild_all)
    session.commit()  # 结束本会话的读事务，好看到线程里刚写进去的行
    return {"ok": True, "added": added, **_status(session)}
