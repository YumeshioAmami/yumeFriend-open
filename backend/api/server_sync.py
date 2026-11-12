"""轻量服务器模式的数据交换端点（v7.12 / D342）。

服务器只做权威数据源 + 增量交换，不跑 LLM、不跑生活循环。
客户端通过 `YF_SERVER_TOKEN` 共享密钥访问。
"""

from __future__ import annotations

import os
import secrets
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import unquote

from fastapi import APIRouter, Header, HTTPException, Request
from fastapi.responses import Response
from pydantic import BaseModel

from backend.db import session_scope
from backend import config
from backend.models import SyncBlob
from backend.services import sync_service

router = APIRouter(prefix="/api/server-sync", tags=["server-sync"])


class ExchangeIn(BaseModel):
    bundle: dict
    since: str | None = None


class ReplaceChatIn(BaseModel):
    bundle: dict


def _backup_db() -> str:
    backup_dir = Path(config.USER_DATA_DIR) / "backups"
    backup_dir.mkdir(parents=True, exist_ok=True)
    target = backup_dir / (
        "yumefriend-before-replace-chat-"
        + datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        + ".db"
    )
    source = sqlite3.connect(str(config.DB_PATH))
    dest = sqlite3.connect(str(target))
    try:
        source.backup(dest)
    finally:
        dest.close()
        source.close()
    return str(target)


def _token() -> str:
    return (os.environ.get("YF_SERVER_TOKEN") or "").strip()


def _require_token(provided: str | None) -> None:
    expected = _token()
    if not expected:
        raise HTTPException(
            status_code=503,
            detail="服务器模式未配置 YF_SERVER_TOKEN —— 先给服务器设一个共享密钥",
        )
    if not provided or not secrets.compare_digest(provided, expected):
        raise HTTPException(status_code=403, detail="服务器同步密钥不对")


@router.get("/ping")
def ping(x_yf_server_token: str | None = Header(default=None)) -> dict:
    _require_token(x_yf_server_token)
    return {"ok": True, "app": "yumeFriend", "server_time": datetime.now(timezone.utc).isoformat()}


@router.get("/attachments")
def attachment_manifest(x_yf_server_token: str | None = Header(default=None)) -> dict:
    _require_token(x_yf_server_token)
    return {"ok": True, "attachments": sync_service.attachment_manifest()}


@router.get("/blob/{sha256}")
def download_blob(
    sha256: str,
    x_yf_server_token: str | None = Header(default=None),
) -> Response:
    _require_token(x_yf_server_token)
    with session_scope() as session:
        row = session.get(SyncBlob, sha256)
        if row is not None and row.path:
            path = sync_service.blob_path(row.path)
            if path is not None:
                return Response(
                    content=path.read_bytes(),
                    media_type=row.mime or "application/octet-stream",
                )
    for item in sync_service.attachment_manifest():
        if str(item.get("sha256")) != sha256:
            continue
        path = sync_service.blob_path(str(item.get("path") or ""))
        if path is None:
            break
        return Response(
            content=path.read_bytes(),
            media_type=str(item.get("mime") or "application/octet-stream"),
        )
    raise HTTPException(status_code=404, detail="没有这个附件")


@router.post("/blob/{sha256}")
async def upload_blob(
    sha256: str,
    request: Request,
    x_yf_server_token: str | None = Header(default=None),
    x_yf_path: str | None = Header(default=None),
) -> dict:
    _require_token(x_yf_server_token)
    rel_path = unquote((x_yf_path or "").strip())
    if not rel_path:
        raise HTTPException(status_code=400, detail="缺少 X-YF-Path")
    data = await request.body()
    try:
        safe = sync_service.write_attachment(rel_path, data, sha256)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    with session_scope() as session:
        sync_service.record_blob(
            session,
            sha256,
            len(data),
            request.headers.get("content-type") or "application/octet-stream",
            safe,
        )
    return {"ok": True, "path": safe, "size": len(data)}


@router.get("/full-chat")
def full_chat(x_yf_server_token: str | None = Header(default=None)) -> dict:
    _require_token(x_yf_server_token)
    with session_scope() as session:
        bundle = sync_service.export_chat_bundle(session)
        server_time = datetime.now(timezone.utc).isoformat()
    return {"ok": True, "bundle": bundle, "server_time": server_time}


@router.post("/replace-chat")
def replace_chat(
    body: ReplaceChatIn,
    x_yf_server_token: str | None = Header(default=None),
) -> dict:
    _require_token(x_yf_server_token)
    backup = _backup_db()
    with session_scope() as session:
        result = sync_service.replace_chat_bundle(session, body.bundle)
        sync_service.mark_all_messages_relayed(session)
        server_time = datetime.now(timezone.utc).isoformat()
    return {
        "ok": True,
        "imported": result.get("imported", {}),
        "backup": backup,
        "server_time": server_time,
    }


@router.get("/full-memories")
def full_memories(x_yf_server_token: str | None = Header(default=None)) -> dict:
    _require_token(x_yf_server_token)
    with session_scope() as session:
        bundle = sync_service.export_memory_bundle(session)
        server_time = datetime.now(timezone.utc).isoformat()
    return {"ok": True, "bundle": bundle, "server_time": server_time}


@router.post("/replace-memories")
def replace_memories(
    body: ReplaceChatIn,
    x_yf_server_token: str | None = Header(default=None),
) -> dict:
    _require_token(x_yf_server_token)
    backup = _backup_db()
    with session_scope() as session:
        result = sync_service.replace_memory_bundle(session, body.bundle)
        server_time = datetime.now(timezone.utc).isoformat()
    return {
        "ok": True,
        "imported": result.get("imported", {}),
        "backup": backup,
        "server_time": server_time,
    }


@router.post("/exchange")
def exchange(
    body: ExchangeIn,
    x_yf_server_token: str | None = Header(default=None),
) -> dict:
    """客户端送来本地增量，服务器合并后回送服务器增量。"""
    _require_token(x_yf_server_token)
    with session_scope() as session:
        merged = sync_service.merge_bundle(
            session,
            body.bundle,
            relay_incoming_messages=True,
        )
        outgoing = sync_service.export_bundle(
            session,
            since=body.since,
            include_attachments=False,
            use_outbox=False,
            use_relay=True,
        )
        server_time = datetime.now(timezone.utc).isoformat()
    return {
        "ok": True,
        "merged": merged,
        "bundle": outgoing,
        "server_time": server_time,
    }
