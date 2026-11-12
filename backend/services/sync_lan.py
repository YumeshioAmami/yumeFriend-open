"""临时局域网同步服务 + 客户端（D302）。"""

from __future__ import annotations

import json
import secrets
import socket
import threading
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from backend.db import session_scope
from backend.services import discovery_service, sync_service

_server: ThreadingHTTPServer | None = None
_thread: threading.Thread | None = None
_code = ""
_info: dict = {}
VALID_DIRECTIONS = ("both", "pull", "push")


def _json_bytes(payload: Any) -> bytes:
    return json.dumps(payload, ensure_ascii=False).encode("utf-8")


def _local_ip() -> str:
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.settimeout(0.5)
        sock.connect(("8.8.8.8", 80))
        ip = sock.getsockname()[0]
        sock.close()
        return ip
    except OSError:
        return "127.0.0.1"


class _Handler(BaseHTTPRequestHandler):
    server_version = "yumeFriendSync/1"

    def log_message(self, _format: str, *_args) -> None:
        return

    def _send(self, status: int, payload: dict | bytes, content_type: str = "application/json") -> None:
        body = payload if isinstance(payload, bytes) else _json_bytes(payload)
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _authorized(self) -> bool:
        return secrets.compare_digest(self.headers.get("X-YF-Sync-Code", ""), _code)

    def do_GET(self) -> None:  # noqa: N802
        if self.path.startswith("/ping"):
            with session_scope() as session:
                self._send(
                    200,
                    {
                        "ok": True,
                        "app": "yumeFriend",
                        "device_id": sync_service.device_id(session),
                        "device_name": sync_service.device_name(session),
                        "device_kind": sync_service.device_kind(session),
                    },
                )
            return
        if not self._authorized():
            self._send(403, {"ok": False, "error": "同步码不对"})
            return
        if self.path.startswith("/bundle"):
            with session_scope() as session:
                self._send(200, sync_service.export_bundle(session))
            return
        if self.path.startswith("/blob/"):
            sha = self.path.split("/blob/", 1)[1].split("?", 1)[0]
            found = None
            for item in sync_service.attachment_manifest():
                if item["sha256"] == sha:
                    found = item
                    break
            if found is None:
                self._send(404, {"ok": False, "error": "没有这个附件"})
                return
            path = sync_service.blob_path(found["path"])
            if path is None:
                self._send(404, {"ok": False, "error": "附件文件不在了"})
                return
            self._send(200, path.read_bytes(), found.get("mime") or "application/octet-stream")
            return
        self._send(404, {"ok": False, "error": "not found"})

    def do_POST(self) -> None:  # noqa: N802
        if not self._authorized():
            self._send(403, {"ok": False, "error": "同步码不对"})
            return
        length = int(self.headers.get("Content-Length", "0") or "0")
        body = self.rfile.read(length)
        if self.path.startswith("/bundle"):
            try:
                bundle = json.loads(body.decode("utf-8"))
                with session_scope() as session:
                    result = sync_service.merge_bundle(session, bundle)
                self._send(200, result)
            except Exception as exc:  # noqa: BLE001
                self._send(400, {"ok": False, "error": str(exc)})
            return
        if self.path.startswith("/blob/"):
            sha = self.path.split("/blob/", 1)[1].split("?", 1)[0]
            rel_path = urllib.parse.unquote(self.headers.get("X-YF-Path", ""))
            try:
                sync_service.write_attachment(rel_path, body, sha)
                with session_scope() as session:
                    sync_service.record_blob(
                        session,
                        sha,
                        len(body),
                        self.headers.get("Content-Type") or "application/octet-stream",
                        rel_path,
                    )
                self._send(200, {"ok": True})
            except Exception as exc:  # noqa: BLE001
                self._send(400, {"ok": False, "error": str(exc)})
            return
        self._send(404, {"ok": False, "error": "not found"})


def _start_server_thread(server: ThreadingHTTPServer) -> None:
    thread = threading.Thread(target=server.serve_forever, daemon=True, name="yf-sync-lan")
    thread.start()


def start(device_name: str, device_kind: str) -> dict:
    """开始等待同步；返回 6 位码、端口、地址和发现的设备。"""
    global _server, _thread, _code, _info
    if _server is not None:
        return dict(_info)
    with session_scope() as session:
        sync_service.configure_device(session, name=device_name, kind=device_kind)
    _code = f"{secrets.randbelow(1_000_000):06d}"
    server = ThreadingHTTPServer(("0.0.0.0", 0), _Handler)
    server.daemon_threads = True
    _server = server
    _thread = threading.Thread(target=server.serve_forever, daemon=True, name="yf-sync-lan")
    _thread.start()
    port = int(server.server_address[1])
    with session_scope() as session:
        info = {
            "ok": True,
            "code": _code,
            "port": port,
            "ip": _local_ip(),
            "device_id": sync_service.device_id(session),
            "device_name": sync_service.device_name(session),
            "device_kind": sync_service.device_kind(session),
        }
    try:
        discovery_service.start(info["device_id"], info["device_name"], info["device_kind"], port)
    except Exception:  # noqa: BLE001 —— 发现失败不影响手动地址/直接 peer
        pass
    _info = info
    return dict(_info)


def stop() -> dict:
    global _server, _thread, _info
    discovery_service.stop()
    if _server is not None:
        try:
            _server.shutdown()
            _server.server_close()
        except Exception:  # noqa: BLE001
            pass
    _server = None
    _thread = None
    _info = {}
    return {"ok": True}


def status() -> dict:
    if _server is None:
        return {"running": False}
    return {"running": True, **_info}


def _http_json(url: str, code: str, payload: dict | None = None, timeout: int = 20) -> dict:
    data = _json_bytes(payload) if payload is not None else None
    request = urllib.request.Request(
        url,
        data=data,
        method="POST" if payload is not None else "GET",
        headers={
            "X-YF-Sync-Code": code,
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise ValueError(f"同步请求失败（HTTP {exc.code}）：{detail[:2000]}") from exc


def _download_blob(base: str, code: str, item: dict) -> None:
    request = urllib.request.Request(
        f"{base}/blob/{item['sha256']}",
        headers={"X-YF-Sync-Code": code},
    )
    with urllib.request.urlopen(request, timeout=60) as response:
        data = response.read()
    sync_service.write_attachment(str(item["path"]), data, str(item["sha256"]))
    with session_scope() as session:
        sync_service.record_blob(
            session,
            str(item["sha256"]),
            len(data),
            str(item.get("mime") or "application/octet-stream"),
            str(item["path"]),
        )


def _upload_blob(base: str, code: str, item: dict) -> None:
    path = sync_service.blob_path(str(item["path"]))
    if path is None:
        return
    data = path.read_bytes()
    request = urllib.request.Request(
        f"{base}/blob/{item['sha256']}",
        data=data,
        method="POST",
        headers={
            "X-YF-Sync-Code": code,
            "X-YF-Path": urllib.parse.quote(str(item["path"]), safe=""),
            "Content-Type": str(item.get("mime") or "application/octet-stream"),
        },
    )
    with urllib.request.urlopen(request, timeout=60):
        pass


def sync_with_peer(peer: dict, code: str, direction: str = "both") -> dict:
    direction = str(direction or "both").strip().lower()
    if direction not in VALID_DIRECTIONS:
        raise ValueError("同步方向只能是 both / pull / push")
    ip = str(peer.get("ip") or "").strip()
    port = int(peer.get("port") or 0)
    if not ip or not port:
        raise ValueError("没有拿到对方地址")
    base = f"http://{ip}:{port}"
    remote_info: dict = {}
    pulled: dict = {}
    pushed: dict = {}

    if direction in ("both", "pull"):
        remote = _http_json(f"{base}/bundle", code, timeout=30)
        remote_info = {
            "device_id": remote.get("device_id"),
            "device_name": remote.get("device_name"),
            "device_kind": remote.get("device_kind"),
        }
        with session_scope() as session:
            if remote_info["device_id"]:
                sync_service.remember_device(
                    session,
                    {
                        "device_id": remote_info["device_id"],
                        "name": remote_info["device_name"],
                        "kind": remote_info["device_kind"],
                    },
                )
            result = sync_service.merge_bundle(session, remote)
        for item in result.get("attachments", []):
            _download_blob(base, code, item)
        pulled = result.get("imported", {})

    if direction in ("both", "push"):
        # /ping 只含设备信息，不读对方业务数据；只推送时用它显示对方是谁。
        ping = _http_json(f"{base}/ping", code, timeout=10)
        remote_info = {
            "device_id": ping.get("device_id"),
            "device_name": ping.get("device_name"),
            "device_kind": ping.get("device_kind"),
        }
        with session_scope() as session:
            if remote_info["device_id"]:
                sync_service.remember_device(
                    session,
                    {
                        "device_id": remote_info["device_id"],
                        "name": remote_info["device_name"],
                        "kind": remote_info["device_kind"],
                    },
                )
            local_bundle = sync_service.export_bundle(session)
        result = _http_json(f"{base}/bundle", code, local_bundle, timeout=60)
        for item in result.get("attachments", []):
            _upload_blob(base, code, item)
        pushed = result.get("imported", {})

    return {
        "ok": True,
        "direction": direction,
        "remote": {
            "device_id": remote_info.get("device_id"),
            "device_name": remote_info.get("device_name"),
        },
        "pulled": pulled,
        "pushed": pushed,
    }
