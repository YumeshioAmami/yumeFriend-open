"""Minecraft Java 版服务器状态探测。

只走 Server List Ping（status），不登录、不加载 minecraft-data。用于在
启动适配器之前知道服务器的版本名和协议号，再挑对应的数据包。
"""

from __future__ import annotations

import json
import socket
import struct
import time
from typing import Any


def _encode_varint(value: int) -> bytes:
    value &= 0xFFFFFFFF
    out = bytearray()
    while True:
        byte = value & 0x7F
        value >>= 7
        if value:
            out.append(byte | 0x80)
        else:
            out.append(byte)
            return bytes(out)


def _decode_varint(read_one) -> int:
    value = 0
    shift = 0
    for _ in range(5):
        raw = read_one()
        if raw == b"":
            raise ValueError("服务器提前关闭了连接")
        byte = raw[0]
        value |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return value
        shift += 7
    raise ValueError("响应里的 VarInt 太长")


def _read_exact(sock: socket.socket, size: int) -> bytes:
    chunks: list[bytes] = []
    remaining = size
    while remaining > 0:
        chunk = sock.recv(remaining)
        if not chunk:
            raise ValueError("服务器提前关闭了连接")
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def _packet(payload: bytes) -> bytes:
    return _encode_varint(len(payload)) + payload


def _mc_string(value: str) -> bytes:
    encoded = value.encode("utf-8")
    return _encode_varint(len(encoded)) + encoded


def ping_server(host: str, port: int, *, timeout: float = 6.0) -> dict[str, Any]:
    host = (host or "").strip()
    if not host:
        raise ValueError("服务器地址不能为空")
    if not 1 <= int(port) <= 65535:
        raise ValueError("服务器端口不对")

    started = time.monotonic()
    try:
        with socket.create_connection((host, int(port)), timeout=timeout) as sock:
            sock.settimeout(timeout)
            # protocol -1 表示只做状态查询；服务器只需要 next_state=1。
            handshake = (
                b"\x00"
                + _encode_varint(-1)
                + _mc_string(host)
                + struct.pack(">H", int(port))
                + _encode_varint(1)
            )
            sock.sendall(_packet(handshake))
            sock.sendall(_packet(b"\x00"))

            def read_one() -> bytes:
                return sock.recv(1)

            packet_length = _decode_varint(read_one)
            packet = _read_exact(sock, packet_length)
            offset = 0

            def read_packet_varint() -> int:
                nonlocal offset

                def one() -> bytes:
                    nonlocal offset
                    if offset >= len(packet):
                        return b""
                    byte = packet[offset : offset + 1]
                    offset += 1
                    return byte

                return _decode_varint(one)

            packet_id = read_packet_varint()
            if packet_id != 0:
                raise ValueError(f"服务器返回了未知状态包（id={packet_id}）")
            json_size = read_packet_varint()
            if offset + json_size > len(packet):
                raise ValueError("服务器状态 JSON 长度不合法")
            raw = packet[offset : offset + json_size]
            data = json.loads(raw.decode("utf-8", "replace"))
    except (OSError, TimeoutError) as exc:
        raise ValueError(
            f"连不上服务器 {host}:{port} —— 世界开着吗？（{exc}）"
        ) from exc

    version = data.get("version") if isinstance(data, dict) else None
    if not isinstance(version, dict):
        raise ValueError("服务器状态里没有版本信息")
    protocol = version.get("protocol")
    return {
        "ok": True,
        "server_version": str(version.get("name") or ""),
        "protocol": int(protocol) if isinstance(protocol, (int, float)) else None,
        "latency_ms": round((time.monotonic() - started) * 1000),
        "players": data.get("players") if isinstance(data.get("players"), dict) else None,
    }
