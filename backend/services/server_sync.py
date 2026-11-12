"""服务器模式客户端：自动增量拉取 / 合并（v7.12 / D342）。

设计取舍：
  · 服务器是权威数据源，但客户端仍保留本地 SQLite 缓存；
  · 不是 WebSocket，而是 2 秒一次的增量交换 —— 对单用户场景足够“实时”，
    也避免在轻量服务器上维护长连接；
  · 服务器不生成消息；聊天由当前设备本地 Agent 回复。
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx
from loguru import logger
from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.db import session_scope
from backend import config
from backend.models import SyncMeta
from backend.services import sync_service

MODE_KEY = "sync_mode"
URL_KEY = "server_url"
TOKEN_KEY = "server_token"
CURSOR_KEY = "server_cursor"
LAST_AT_KEY = "server_last_sync_at"
LAST_ERROR_KEY = "server_last_error"
PAUSED_KEY = "server_sync_paused"

INTERVAL_SECONDS = 2.0

_task: asyncio.Task | None = None
_task_loop: asyncio.AbstractEventLoop | None = None
# 自动交换和手动全量覆盖互斥：点“暂停”时会等当前这一轮交换真正结束，
# 避免在途请求事后把旧聊天又合并回来。
_op_lock = threading.Lock()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _get(session: Session, key: str, default: str = "") -> str:
    row = session.get(SyncMeta, key)
    return row.value if row is not None else default


def _set(session: Session, key: str, value: str) -> None:
    row = session.get(SyncMeta, key)
    if row is None:
        session.add(SyncMeta(key=key, value=value, updated_at=_now()))
    else:
        row.value = value
        row.updated_at = _now()
    session.flush()


def configure(
    session: Session,
    *,
    mode: str,
    base_url: str | None = None,
    token: str | None = None,
) -> dict[str, Any]:
    mode = (mode or "local").strip().lower()
    if mode not in ("local", "server"):
        raise ValueError("同步模式只能是 local 或 server")
    old_url = _get(session, URL_KEY, "")
    _set(session, MODE_KEY, mode)
    if base_url is not None:
        next_url = base_url.strip().rstrip("/")
        _set(session, URL_KEY, next_url)
        if next_url != old_url:
            # 换服务器 = 新世界：不能拿旧游标去新库要增量。
            _set(session, CURSOR_KEY, "")
    if token is not None:
        _set(session, TOKEN_KEY, token.strip())
    if mode == "server":
        _set(session, PAUSED_KEY, "0")
    _set(session, LAST_ERROR_KEY, "")
    return status(session)


def status(session: Session) -> dict[str, Any]:
    return {
        "mode": _get(session, MODE_KEY, "local") or "local",
        "base_url": _get(session, URL_KEY, ""),
        "token_set": bool(_get(session, TOKEN_KEY, "")),
        "cursor": _get(session, CURSOR_KEY, ""),
        "last_sync_at": _get(session, LAST_AT_KEY, ""),
        "last_error": _get(session, LAST_ERROR_KEY, ""),
        "paused": _get(session, PAUSED_KEY, "") == "1",
        "running": _task is not None and not _task.done(),
        "interval_seconds": INTERVAL_SECONDS,
    }


def configured_token(session: Session) -> str:
    return _get(session, TOKEN_KEY, "")


def set_paused(paused: bool) -> dict[str, Any]:
    # 用锁做一次屏障：暂停 = 等当前交换收尾后再返回；恢复 = 等手动覆盖收尾。
    with _op_lock:
        with session_scope() as session:
            _set(session, PAUSED_KEY, "1" if paused else "0")
            return status(session)


def ensure_paused_on_upgrade(session: Session) -> None:
    """手机升级后第一次进入服务器模式时默认暂停，给全量拉取留出安全窗口。"""
    if (
        _get(session, MODE_KEY, "local") == "server"
        and session.get(SyncMeta, PAUSED_KEY) is None
    ):
        _set(session, PAUSED_KEY, "1")


def backup_database(reason: str) -> str:
    """本地库做一份 SQLite 一致性备份；失败就直接抛，不带着风险做覆盖。"""
    backup_dir = Path(config.USER_DATA_DIR) / "backups"
    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    target = backup_dir / f"yumefriend-before-{reason}-{stamp}.db"
    source = sqlite3.connect(str(config.DB_PATH))
    dest = sqlite3.connect(str(target))
    try:
        source.backup(dest)
    finally:
        dest.close()
        source.close()
    return str(target)


def _server_config(session: Session) -> tuple[str, str, str]:
    mode = _get(session, MODE_KEY, "local") or "local"
    base_url = _get(session, URL_KEY, "").strip().rstrip("/")
    token = _get(session, TOKEN_KEY, "")
    return mode, base_url, token


def _require_manual_ready(session: Session) -> tuple[str, str]:
    mode, base_url, token = _server_config(session)
    if mode != "server":
        raise ValueError("当前不是服务器模式")
    if not base_url or not token:
        raise ValueError("服务器地址或密钥没配置")
    if _get(session, PAUSED_KEY, "") != "1":
        raise ValueError("先暂停自动交换，再做全量覆盖")
    return base_url, token


def test_connection(base_url: str, token: str) -> dict[str, Any]:
    url = (base_url or "").strip().rstrip("/")
    if not url:
        raise ValueError("先填服务器地址")
    with httpx.Client(timeout=httpx.Timeout(12.0, connect=6.0), trust_env=True) as client:
        response = client.get(
            f"{url}/api/server-sync/ping",
            headers={"X-YF-Server-Token": token or ""},
        )
    if response.status_code >= 400:
        raise ValueError(f"服务器回了 HTTP {response.status_code}：{response.text[:300]}")
    payload = response.json()
    if not payload.get("ok"):
        raise ValueError("服务器没有返回 ok")
    return payload


def _attachment_key(item: dict) -> tuple[str, str]:
    return (str(item.get("path") or ""), str(item.get("sha256") or ""))


def _remote_manifest(
    client: httpx.Client, base_url: str, token: str
) -> list[dict]:
    response = client.get(
        f"{base_url}/api/server-sync/attachments",
        headers={"X-YF-Server-Token": token},
    )
    if response.status_code >= 400:
        raise RuntimeError(
            f"附件清单失败 HTTP {response.status_code}: {response.text[:300]}"
        )
    payload = response.json()
    if not payload.get("ok"):
        raise RuntimeError(str(payload)[:300])
    items = payload.get("attachments") or []
    return [item for item in items if isinstance(item, dict)]


def _download_attachment(
    client: httpx.Client, base_url: str, token: str, item: dict
) -> None:
    sha = str(item.get("sha256") or "")
    rel_path = str(item.get("path") or "")
    response = client.get(
        f"{base_url}/api/server-sync/blob/{sha}",
        headers={"X-YF-Server-Token": token},
    )
    if response.status_code >= 400:
        raise RuntimeError(
            f"下载附件失败 HTTP {response.status_code}: {rel_path}"
        )
    sync_service.write_attachment(rel_path, response.content, sha)
    with session_scope() as session:
        sync_service.record_blob(
            session,
            sha,
            len(response.content),
            str(item.get("mime") or "application/octet-stream"),
            rel_path,
        )


def _upload_attachment(
    client: httpx.Client, base_url: str, token: str, item: dict
) -> None:
    sha = str(item.get("sha256") or "")
    rel_path = str(item.get("path") or "")
    path = sync_service.blob_path(rel_path)
    if path is None:
        raise RuntimeError(f"本地附件不见了：{rel_path}")
    response = client.post(
        f"{base_url}/api/server-sync/blob/{sha}",
        content=path.read_bytes(),
        headers={
            "X-YF-Server-Token": token,
            "X-YF-Path": quote(rel_path, safe=""),
            "Content-Type": str(item.get("mime") or "application/octet-stream"),
        },
    )
    if response.status_code >= 400:
        raise RuntimeError(
            f"上传附件失败 HTTP {response.status_code}: {rel_path} {response.text[:200]}"
        )


def sync_attachments(
    base_url: str, token: str, *, direction: str = "both"
) -> dict[str, Any]:
    """同步 user_data 下的头像/立绘/背景/聊天图/知识库原件。

    按 `(path, sha256)` 比较，内容一样不重传；和 LAN 同步共用同一套附件白名单。
    """
    direction = str(direction or "both").strip().lower()
    if direction not in ("both", "pull", "push"):
        raise ValueError("附件同步方向只能是 both / pull / push")
    base_url = (base_url or "").strip().rstrip("/")
    if not base_url:
        raise ValueError("先填服务器地址")
    local = sync_service.attachment_manifest()
    local_keys = {_attachment_key(item) for item in local}
    downloaded = 0
    uploaded = 0
    errors: list[str] = []
    with httpx.Client(
        timeout=httpx.Timeout(120.0, connect=10.0),
        trust_env=True,
    ) as client:
        remote = _remote_manifest(client, base_url, token)
        remote_keys = {_attachment_key(item) for item in remote}
        if direction in ("both", "pull"):
            for item in remote:
                if _attachment_key(item) in local_keys:
                    continue
                try:
                    _download_attachment(client, base_url, token, item)
                    downloaded += 1
                except Exception as exc:  # noqa: BLE001
                    errors.append(str(exc)[:300])
        if direction in ("both", "push"):
            for item in local:
                if _attachment_key(item) in remote_keys:
                    continue
                try:
                    _upload_attachment(client, base_url, token, item)
                    uploaded += 1
                except Exception as exc:  # noqa: BLE001
                    errors.append(str(exc)[:300])
    return {
        "ok": not errors,
        "direction": direction,
        "downloaded": downloaded,
        "uploaded": uploaded,
        "local": len(local),
        "remote": len(remote),
        "errors": errors[:8],
    }


def exchange_once() -> dict[str, Any]:
    with _op_lock:
        return _exchange_once_locked()


def _exchange_once_locked() -> dict[str, Any]:
    # 阶段 1：只读配置 + 导出本地增量。**导出完立刻结束事务**，别让网络请求
    # 陪着 SQLite 会话一起挂几十秒（D197 的同类坑）。
    with session_scope() as session:
        mode = _get(session, MODE_KEY, "local") or "local"
        if mode != "server":
            return {"ok": True, "skipped": "local_mode"}
        if _get(session, PAUSED_KEY, "") == "1":
            return {"ok": True, "skipped": "paused"}
        base_url = _get(session, URL_KEY, "").strip().rstrip("/")
        token = _get(session, TOKEN_KEY, "")
        if not base_url or not token:
            _set(session, LAST_ERROR_KEY, "服务器地址或密钥没配置")
            return {"ok": False, "error": "服务器地址或密钥没配置"}
        cursor = _get(session, CURSOR_KEY, "") or None
        local_bundle = sync_service.export_bundle(
            session,
            since=cursor,
            include_attachments=False,
        )
        sent_message_ids = [
            str(row.get("id"))
            for row in (local_bundle.get("tables") or {}).get("messages") or []
            if row.get("id")
        ]

    # 阶段 2：纯网络 I/O，不持有任何数据库会话。
    try:
        with httpx.Client(
            timeout=httpx.Timeout(30.0, connect=8.0),
            trust_env=True,
        ) as client:
            response = client.post(
                f"{base_url}/api/server-sync/exchange",
                headers={"X-YF-Server-Token": token},
                json={"bundle": local_bundle, "since": cursor},
            )
        if response.status_code >= 400:
            raise RuntimeError(f"HTTP {response.status_code}: {response.text[:500]}")
        payload = response.json()
        if not payload.get("ok"):
            raise RuntimeError(str(payload)[:500])
        remote_bundle = payload.get("bundle") or {}
        next_cursor = str(
            remote_bundle.get("generated_at")
            or payload.get("server_time")
            or _now()
        )
    except Exception as exc:  # noqa: BLE001 —— 网络失败只记录，不拖垮应用
        message = str(exc)[:800]
        with session_scope() as session:
            _set(session, LAST_ERROR_KEY, message)
        logger.warning("服务器同步失败：{}", message)
        return {"ok": False, "error": message}

    # 阶段 3：合并远端包 + 推进游标；这期间不碰网络。
    try:
        with session_scope() as session:
            if remote_bundle:
                sync_service.merge_bundle(session, remote_bundle)
            sync_service.ack_messages(session, sent_message_ids)
            _set(session, CURSOR_KEY, next_cursor)
            _set(session, LAST_AT_KEY, _now())
            _set(session, LAST_ERROR_KEY, "")
            session.flush()
        try:
            attachments = sync_attachments(base_url, token, direction="both")
        except Exception as exc:  # noqa: BLE001 —— 附件失败不推翻已成功的数据交换
            attachments = {"ok": False, "error": str(exc)[:500]}
            logger.warning("服务器同步附件失败（数据已同步）：{}", exc)
        if not attachments.get("ok"):
            with session_scope() as session:
                _set(
                    session,
                    LAST_ERROR_KEY,
                    "附件同步失败：" + str(attachments.get("error") or attachments)[:600],
                )
        return {"ok": True, "cursor": next_cursor, "attachments": attachments}
    except Exception as exc:  # noqa: BLE001 —— 合并失败只记录
        message = str(exc)[:800]
        with session_scope() as session:
            _set(session, LAST_ERROR_KEY, message)
        logger.warning("服务器同步合并失败：{}", message)
        return {"ok": False, "error": message}


def push_chat() -> dict[str, Any]:
    """把本机完整聊天/记忆快照覆盖到服务器；必须已暂停自动交换。"""
    with _op_lock:
        return _push_chat_locked()


def _push_chat_locked() -> dict[str, Any]:
    with session_scope() as session:
        try:
            base_url, token = _require_manual_ready(session)
        except ValueError as exc:
            return {"ok": False, "error": str(exc)}
        local_bundle = sync_service.export_chat_bundle(session)
    try:
        with httpx.Client(
            timeout=httpx.Timeout(60.0, connect=8.0), trust_env=True
        ) as client:
            response = client.post(
                f"{base_url}/api/server-sync/replace-chat",
                headers={"X-YF-Server-Token": token},
                json={"bundle": local_bundle},
            )
        if response.status_code >= 400:
            raise RuntimeError(f"HTTP {response.status_code}: {response.text[:500]}")
        payload = response.json()
        if not payload.get("ok"):
            raise RuntimeError(str(payload)[:500])
        next_cursor = str(payload.get("server_time") or _now())
    except Exception as exc:  # noqa: BLE001
        message = str(exc)[:800]
        with session_scope() as session:
            _set(session, LAST_ERROR_KEY, message)
        logger.warning("全量推送聊天/记忆失败：{}", message)
        return {"ok": False, "error": message}
    with session_scope() as session:
        sync_service.clear_message_outbox(session)
        _set(session, CURSOR_KEY, next_cursor)
        _set(session, LAST_AT_KEY, _now())
        _set(session, LAST_ERROR_KEY, "")
    try:
        attachments = sync_attachments(base_url, token, direction="push")
    except Exception as exc:  # noqa: BLE001
        attachments = {"ok": False, "error": str(exc)[:500]}
        logger.warning("手动推送附件失败（数据已推送）：{}", exc)
    return {
        "ok": True,
        "direction": "push_chat",
        "cursor": next_cursor,
        "backup": payload.get("backup"),
        "imported": payload.get("imported"),
        "attachments": attachments,
    }


def pull_chat() -> dict[str, Any]:
    """用服务器完整聊天/记忆快照覆盖本机；先备份本机库。"""
    with _op_lock:
        return _pull_chat_locked()


def _pull_chat_locked() -> dict[str, Any]:
    with session_scope() as session:
        try:
            base_url, token = _require_manual_ready(session)
        except ValueError as exc:
            return {"ok": False, "error": str(exc)}
    try:
        with httpx.Client(
            timeout=httpx.Timeout(60.0, connect=8.0), trust_env=True
        ) as client:
            response = client.get(
                f"{base_url}/api/server-sync/full-chat",
                headers={"X-YF-Server-Token": token},
            )
        if response.status_code >= 400:
            raise RuntimeError(f"HTTP {response.status_code}: {response.text[:500]}")
        payload = response.json()
        if not payload.get("ok"):
            raise RuntimeError(str(payload)[:500])
        remote_bundle = payload.get("bundle") or {}
        next_cursor = str(payload.get("server_time") or _now())
    except Exception as exc:  # noqa: BLE001
        message = str(exc)[:800]
        with session_scope() as session:
            _set(session, LAST_ERROR_KEY, message)
        logger.warning("全量拉取聊天/记忆失败：{}", message)
        return {"ok": False, "error": message}
    backup = ""
    try:
        backup = backup_database("pull-chat")
        with session_scope() as session:
            result = sync_service.replace_chat_bundle(session, remote_bundle)
            sync_service.clear_message_outbox(session)
            _set(session, CURSOR_KEY, next_cursor)
            _set(session, LAST_AT_KEY, _now())
            _set(session, LAST_ERROR_KEY, "")
    except Exception as exc:  # noqa: BLE001
        message = str(exc)[:800]
        with session_scope() as session:
            _set(session, LAST_ERROR_KEY, message)
        logger.warning("全量拉取聊天/记忆合并失败（本机备份 {}）：{}", backup, message)
        return {"ok": False, "error": message, "backup": backup}
    try:
        attachments = sync_attachments(base_url, token, direction="pull")
    except Exception as exc:  # noqa: BLE001
        attachments = {"ok": False, "error": str(exc)[:500]}
        logger.warning("手动拉取附件失败（数据已拉取）：{}", exc)
    return {
        "ok": True,
        "direction": "pull_chat",
        "cursor": next_cursor,
        "backup": backup,
        "imported": result.get("imported"),
        "attachments": attachments,
    }


def push_memories() -> dict[str, Any]:
    """只把本机记忆范围覆盖到服务器；必须已暂停自动交换。"""
    with _op_lock:
        return _push_memories_locked()


def _push_memories_locked() -> dict[str, Any]:
    with session_scope() as session:
        try:
            base_url, token = _require_manual_ready(session)
        except ValueError as exc:
            return {"ok": False, "error": str(exc)}
        local_bundle = sync_service.export_memory_bundle(session)
    try:
        with httpx.Client(
            timeout=httpx.Timeout(60.0, connect=8.0), trust_env=True
        ) as client:
            response = client.post(
                f"{base_url}/api/server-sync/replace-memories",
                headers={"X-YF-Server-Token": token},
                json={"bundle": local_bundle},
            )
        if response.status_code >= 400:
            raise RuntimeError(f"HTTP {response.status_code}: {response.text[:500]}")
        payload = response.json()
        if not payload.get("ok"):
            raise RuntimeError(str(payload)[:500])
        next_cursor = str(payload.get("server_time") or _now())
    except Exception as exc:  # noqa: BLE001
        message = str(exc)[:800]
        with session_scope() as session:
            _set(session, LAST_ERROR_KEY, message)
        logger.warning("推送记忆失败：{}", message)
        return {"ok": False, "error": message}
    with session_scope() as session:
        _set(session, CURSOR_KEY, next_cursor)
        _set(session, LAST_AT_KEY, _now())
        _set(session, LAST_ERROR_KEY, "")
    return {
        "ok": True,
        "direction": "push_memories",
        "cursor": next_cursor,
        "backup": payload.get("backup"),
        "imported": payload.get("imported"),
    }


def pull_memories() -> dict[str, Any]:
    """用服务器记忆范围覆盖本机；先备份本机库。"""
    with _op_lock:
        return _pull_memories_locked()


def _pull_memories_locked() -> dict[str, Any]:
    with session_scope() as session:
        try:
            base_url, token = _require_manual_ready(session)
        except ValueError as exc:
            return {"ok": False, "error": str(exc)}
    try:
        with httpx.Client(
            timeout=httpx.Timeout(60.0, connect=8.0), trust_env=True
        ) as client:
            response = client.get(
                f"{base_url}/api/server-sync/full-memories",
                headers={"X-YF-Server-Token": token},
            )
        if response.status_code >= 400:
            raise RuntimeError(f"HTTP {response.status_code}: {response.text[:500]}")
        payload = response.json()
        if not payload.get("ok"):
            raise RuntimeError(str(payload)[:500])
        remote_bundle = payload.get("bundle") or {}
        next_cursor = str(payload.get("server_time") or _now())
    except Exception as exc:  # noqa: BLE001
        message = str(exc)[:800]
        with session_scope() as session:
            _set(session, LAST_ERROR_KEY, message)
        logger.warning("拉取记忆失败：{}", message)
        return {"ok": False, "error": message}
    backup = ""
    try:
        backup = backup_database("pull-memories")
        with session_scope() as session:
            result = sync_service.replace_memory_bundle(session, remote_bundle)
            _set(session, CURSOR_KEY, next_cursor)
            _set(session, LAST_AT_KEY, _now())
            _set(session, LAST_ERROR_KEY, "")
    except Exception as exc:  # noqa: BLE001
        message = str(exc)[:800]
        with session_scope() as session:
            _set(session, LAST_ERROR_KEY, message)
        logger.warning("拉取记忆合并失败（本机备份 {}）：{}", backup, message)
        return {"ok": False, "error": message, "backup": backup}
    return {
        "ok": True,
        "direction": "pull_memories",
        "cursor": next_cursor,
        "backup": backup,
        "imported": result.get("imported"),
    }


async def _loop() -> None:
    while True:
        try:
            await asyncio.to_thread(exchange_once)
        except Exception as exc:  # noqa: BLE001
            logger.warning("服务器同步循环异常（已吞掉）：{}", exc)
        await asyncio.sleep(INTERVAL_SECONDS)


def start_loop() -> None:
    global _task, _task_loop
    if _task is not None and not _task.done():
        return
    with session_scope() as session:
        if status(session)["mode"] != "server":
            return
    loop = asyncio.get_running_loop()
    _task = loop.create_task(_loop(), name="yf-server-sync")
    _task_loop = loop
    logger.info("服务器同步循环已启动（{}s 增量交换）", INTERVAL_SECONDS)


async def stop_loop() -> None:
    global _task, _task_loop
    if _task is None:
        return
    _task.cancel()
    try:
        await _task
    except (asyncio.CancelledError, RuntimeError):
        pass
    _task = None
    _task_loop = None


def restart_loop() -> None:
    global _task, _task_loop
    with session_scope() as session:
        mode = status(session)["mode"]
    if _task is not None and not _task.done():
        _task.cancel()
    _task = None
    _task_loop = None
    if mode != "server":
        return
    loop = asyncio.get_running_loop()
    _task = loop.create_task(_loop(), name="yf-server-sync")
    _task_loop = loop
