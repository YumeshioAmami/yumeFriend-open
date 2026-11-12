"""局域网设备发现（D302）。"""

from __future__ import annotations

import json
import socket
import threading
import time

DISCOVERY_PORT = 48000
_lock = threading.Lock()
_responder: socket.socket | None = None
_responder_thread: threading.Thread | None = None
_info: dict = {}


def _announce_payload() -> bytes:
    return json.dumps(
        {
            "app": "yumeFriend",
            "type": "announce",
            **_info,
        },
        ensure_ascii=False,
    ).encode("utf-8")


def _responder_loop(sock: socket.socket) -> None:
    while True:
        try:
            data, addr = sock.recvfrom(4096)
        except OSError:
            return
        if not data.startswith(b"YF_DISCOVER"):
            continue
        try:
            sock.sendto(_announce_payload(), addr)
        except OSError:
            pass


def start(device_id: str, name: str, kind: str, port: int) -> dict:
    """启动发现响应器；只在同步页打开时调用。"""
    global _responder, _responder_thread, _info
    with _lock:
        _info = {
            "device_id": device_id,
            "name": name,
            "kind": kind,
            "port": int(port),
        }
        if _responder is not None:
            return dict(_info)
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        # 允许同一台机器上开发版 / 打包版同时开（端口共享）
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
        except (AttributeError, OSError):
            pass
        try:
            sock.bind(("0.0.0.0", DISCOVERY_PORT))
        except OSError:
            sock.close()
            _responder = None
            return dict(_info)
        _responder = sock
        _responder_thread = threading.Thread(
            target=_responder_loop, args=(sock,), daemon=True, name="yf-discovery"
        )
        _responder_thread.start()
        return dict(_info)


def stop() -> None:
    global _responder, _responder_thread, _info
    with _lock:
        if _responder is not None:
            try:
                _responder.close()
            except OSError:
                pass
        _responder = None
        _responder_thread = None
        _info = {}


def discover(timeout: float = 1.5) -> list[dict]:
    """广播一次并收集 announce；正常流程只在这个函数里用。"""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    sock.settimeout(0.15)
    try:
        packet = b"YF_DISCOVER"
        # 255.255.255.255 在部分路由器上不转发；再补发常见网段的定向广播。
        targets = ["255.255.255.255"]
        try:
            local_ip = sock.getsockname()[0]
            if local_ip and not local_ip.startswith("127."):
                parts = local_ip.split(".")
                if len(parts) == 4:
                    targets.append(".".join(parts[:3] + ["255"]))
        except OSError:
            pass
        for target in set(targets):
            try:
                sock.sendto(packet, (target, DISCOVERY_PORT))
            except OSError:
                pass
        deadline = time.monotonic() + timeout
        peers: dict[str, dict] = {}
        while time.monotonic() < deadline:
            try:
                data, addr = sock.recvfrom(4096)
            except socket.timeout:
                continue
            try:
                payload = json.loads(data.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                continue
            if payload.get("app") != "yumeFriend" or payload.get("type") != "announce":
                continue
            peer_id = str(payload.get("device_id") or "")
            if not peer_id:
                continue
            payload["ip"] = addr[0]
            peers[peer_id] = payload
        return list(peers.values())
    finally:
        sock.close()
