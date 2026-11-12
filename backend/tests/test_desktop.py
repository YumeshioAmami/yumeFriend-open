"""T5.6 验收的机器部分：单实例锁 + 后端能发出前端页面。"""

import subprocess
import sys
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import backend.config as config
import backend.db as db
from backend.app import create_app
from backend.desktop.single_instance import SingleInstance

ROOT = str(Path(__file__).resolve().parents[2])


def _holder_script(lock_name: str) -> str:
    """子进程：拿住锁 10 秒（用来验证"第二个实例被挡住"）。"""
    return (
        "import sys, time;"
        f"sys.path.insert(0, {ROOT!r});"
        f"from backend.desktop.single_instance import SingleInstance;"
        f"s = SingleInstance({lock_name!r});"
        "assert s.acquire(), 'holder failed';"
        "print('LOCKED', flush=True);"
        "time.sleep(10)"
    )


def test_second_instance_is_blocked():
    """architecture §9：开两个实例会有两个心跳 → 必须挡住第二个。"""
    name = f"yumeFriend.test.{time.time_ns()}"
    holder = subprocess.Popen(
        [sys.executable, "-c", _holder_script(name)],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
    )
    try:
        assert holder.stdout is not None
        assert holder.stdout.readline().strip() == "LOCKED", "子进程没拿到锁"
        # 第二个"实例"拿同一个名字 → 必须失败
        assert SingleInstance(name).acquire() is False
    finally:
        holder.terminate()
        holder.wait(timeout=10)

    # 持锁进程没了 → 又能拿到了（内核自动释放，不留脏锁）。
    # 注意：terminate 返回后内核可能还没完全回收句柄，所以这里轮询等一小会儿。
    deadline = time.monotonic() + 5
    freed = False
    while time.monotonic() < deadline:
        if SingleInstance(name).acquire():
            freed = True
            break
        time.sleep(0.2)
    assert freed, "持锁进程结束后锁没被释放"


def test_backend_serves_the_built_frontend(tmp_path, monkeypatch):
    """桌面窗口要加载页面：后端得把 frontend/dist 发出来，而且**不能抢 /api**。"""
    if not (config.ROOT_DIR / "frontend" / "dist" / "index.html").is_file():
        pytest.skip("还没 build 过前端（cd frontend && npm run build）")

    db_file = tmp_path / "desktop.db"
    monkeypatch.setattr(config, "DB_PATH", db_file)
    monkeypatch.setattr(db, "DB_PATH", db_file)
    monkeypatch.setattr(db, "_engine", None)
    monkeypatch.setattr(db, "_session_factory", None)

    app = create_app()
    with TestClient(app) as client:
        page = client.get("/")
        assert page.status_code == 200
        assert "<div id=\"root\">" in page.text
        # 静态挂载在最后 → /api 仍然是 API，不是文件
        assert client.get("/api/health").json()["ok"] is True
        assert client.get("/api/friends").status_code == 200


def test_backend_serves_the_mobile_shell_too(tmp_path, monkeypatch):
    """手机壳（v3.0 / D140）：同一份构建产物里，`/m/` 出的是**另一个入口**。

    这条盯着三件事：① `/m/` 打得开；② 出的确实是手机壳（不是桌面那份）；
    ③ 手机壳那页引的 JS 是 mobile 那个 chunk —— 不然手机上跑的还是桌面逻辑。
    """
    if not (config.ROOT_DIR / "frontend" / "dist" / "m" / "index.html").is_file():
        pytest.skip("还没 build 过前端（cd frontend && npm run build）")

    db_file = tmp_path / "desktop-mobile.db"
    monkeypatch.setattr(config, "DB_PATH", db_file)
    monkeypatch.setattr(db, "DB_PATH", db_file)
    monkeypatch.setattr(db, "_engine", None)
    monkeypatch.setattr(db, "_session_factory", None)

    app = create_app()
    with TestClient(app) as client:
        page = client.get("/m/")  # 带斜杠（StaticFiles 的目录索引）
        assert page.status_code == 200
        assert '<div id="root">' in page.text
        # 手机壳自己的两个标记：viewport-fit（刘海安全区）+ 它是 mobile 入口
        assert "viewport-fit=cover" in page.text
        assert "/assets/mobile-" in page.text
        # 不带斜杠也要能进（前端/手输地址都可能这么写）
        assert client.get("/m").status_code == 200
        # 桌面那份没被顶掉
        assert "viewport-fit=cover" not in client.get("/").text


# ---------- D91：端口被占就自动换一个 ----------


def test_default_port_is_fixed_8001():
    """D283：桌面版端口固定 8001，手机端地址才稳定。"""
    assert config.PORT == 8001


def test_choose_port_uses_the_preferred_one_when_free():
    import socket

    from backend.desktop import app as desktop_app

    with socket.socket() as probe:
        probe.bind((config.HOST, 0))
        free_port = probe.getsockname()[1]
    port, note = desktop_app.choose_port(free_port)
    assert port == free_port
    assert note is None


def test_choose_port_skips_a_taken_port():
    """兼容路径：`fallback=True` 时被占要往后找（桌面版默认已改为固定端口）。"""
    import socket

    from backend.desktop import app as desktop_app

    taken = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    taken.bind((config.HOST, 0))
    taken.listen(1)
    port_in_use = taken.getsockname()[1]
    try:
        assert desktop_app._port_is_free(port_in_use) is False
        port, note = desktop_app.choose_port(port_in_use, tries=5)
        assert port != port_in_use
        assert port_in_use < port <= port_in_use + 5
        assert note and str(port_in_use) in note and str(port) in note
    finally:
        taken.close()


def test_choose_port_fixed_refuses_to_shift():
    """D283：桌面版固定 8001；被占就返回 None，不静默换端口。"""
    import socket

    from backend.desktop import app as desktop_app

    taken = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    taken.bind((config.HOST, 0))
    taken.listen(1)
    port_in_use = taken.getsockname()[1]
    try:
        assert desktop_app.choose_port(port_in_use, fallback=False) is None
    finally:
        taken.close()


def test_choose_port_gives_up_after_the_tries():
    import socket

    from backend.desktop import app as desktop_app

    # 连续占两个（默认 tries=1 → 只往后看一个）
    socks = []
    try:
        first = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        first.bind((config.HOST, 0))
        first.listen(1)
        base = first.getsockname()[1]
        socks.append(first)
        second = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        second.bind((config.HOST, base + 1))
        second.listen(1)
        socks.append(second)
        assert desktop_app.choose_port(base, tries=1) is None
    finally:
        for sock in socks:
            sock.close()


def test_runtime_port_file_roundtrip(tmp_path, monkeypatch):
    """实际端口要落盘：stop_backend.ps1 靠它找到我们（D283 固定 8001）。"""
    from backend.desktop import app as desktop_app

    monkeypatch.setattr(desktop_app, "RUNTIME_PORT_FILE", tmp_path / "runtime_port.txt")
    desktop_app._write_runtime_port(8123)
    assert (tmp_path / "runtime_port.txt").read_text(encoding="utf-8") == "8123"
    desktop_app._clear_runtime_port()
    assert not (tmp_path / "runtime_port.txt").exists()
