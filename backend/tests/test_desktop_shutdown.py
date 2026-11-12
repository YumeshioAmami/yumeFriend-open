"""退出必须**真的把进程结束掉**（托盘「退出」踩过的真 bug）。

症状：窗口和托盘图标都没了，但 python 还在后台跑 —— 单实例锁和 8000 端口被占着，
再启动永远提示"已经打开了"，而且用户找不到它去关。

这个测试用**子进程**验：调用 shutdown() 之后，进程必须在几秒内真的退出。
"""

import subprocess
import sys
import time
from pathlib import Path

ROOT = str(Path(__file__).resolve().parents[2])


def _spawn(body: str) -> subprocess.Popen:
    code = f"import sys; sys.path.insert(0, r'{ROOT}'); {body}"
    return subprocess.Popen(
        [sys.executable, "-c", code],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
    )


def test_shutdown_kills_the_process():
    proc = _spawn(
        "from backend.desktop.app import shutdown;"
        "print('READY', flush=True);"
        "shutdown()"
    )
    try:
        assert proc.stdout is not None
        assert proc.stdout.readline().strip() == "READY"
        # 正常情况 1 秒内就退（shutdown 里等 0.3s 刷日志）
        proc.wait(timeout=15)
        assert proc.returncode == 0
    finally:
        if proc.poll() is None:
            proc.kill()


def test_shutdown_leaves_a_tray_icon_behind_nothing():
    """就算传了假的 icon/window（它们会抛异常），也必须照样退干净。"""
    proc = _spawn(
        "from backend.desktop.app import shutdown;"
        "print('READY', flush=True);"
        "shutdown(icon=object(), window=object())"
    )
    try:
        assert proc.stdout is not None
        assert proc.stdout.readline().strip() == "READY"
        proc.wait(timeout=15)
        assert proc.returncode == 0
    finally:
        if proc.poll() is None:
            proc.kill()
