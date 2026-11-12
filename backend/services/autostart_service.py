"""开机自启（v3.15 / 2026-09-26 用户要求）。

做成设置项，不偷偷塞自启：`settings.auto_start`（默认关）→ 打开时在 Windows 的
「启动」文件夹放一个 `yumeFriend-autostart.cmd`，关掉时删掉它。

为什么用 .cmd 而不是 .lnk：创建快捷方式要 COM（pywin32），而 .cmd 是纯文本、零依赖、
可读可改（用户自己也能看懂它干了什么）。⚠️ 文件名和内容都只用 ASCII ——
AGENTS §6 的教训：`.bat` 里中文会按控制台代码页读成乱码。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

AUTOSTART_NAME = "yumeFriend-autostart.cmd"


def startup_dir() -> Path | None:
    """Windows 的「启动」文件夹；非 Windows 返回 None（这功能只在 Windows 有意义）。"""
    if sys.platform != "win32":
        return None
    appdata = os.environ.get("APPDATA")
    if not appdata:
        return None
    return Path(appdata) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup"


def _under_pytest() -> bool:
    """D179 的教训：**测试绝不能碰真环境**（那次单测把用户的语音配置写坏了）。
    所以跑 pytest 时，谁也别想动真的「启动」文件夹。"""
    return "pytest" in sys.modules or bool(os.environ.get("PYTEST_CURRENT_TEST"))


def script_path(*, base: Path | None = None) -> Path | None:
    folder = base or startup_dir()
    if folder is None:
        return None
    return folder / AUTOSTART_NAME


def _command() -> str:
    """启动命令：打包版直接起 exe；开发版用 pythonw 起主程序（不带控制台窗口）。"""
    if getattr(sys, "frozen", False):
        return f'start "" "{Path(sys.executable).resolve()}"'
    root = Path(__file__).resolve().parent.parent.parent
    pythonw = root / ".venv" / "Scripts" / "pythonw.exe"
    python = pythonw if pythonw.exists() else (root / ".venv" / "Scripts" / "python.exe")
    return f'start "" "{python}" -m backend.main'


def is_enabled(*, base: Path | None = None) -> bool:
    path = script_path(base=base)
    return bool(path and path.exists())


def apply(enabled: bool, *, base: Path | None = None) -> dict:
    """打开/关闭自启。返回 {"ok": bool, "path": str|None, "hint": str}（不抛）。"""
    if base is None and _under_pytest():
        return {"ok": False, "path": None, "hint": "测试环境不动真·启动文件夹"}
    path = script_path(base=base)
    if path is None:
        return {"ok": False, "path": None, "hint": "只有 Windows 才有开机自启"}
    try:
        if enabled:
            path.parent.mkdir(parents=True, exist_ok=True)
            body = (
                "@echo off\r\n"
                "rem yumeFriend autostart (turn it off in Settings to delete this file)\r\n"
                f"{_command()}\r\n"
            )
            path.write_text(body, encoding="utf-8", newline="")
            return {"ok": True, "path": str(path), "hint": "已加入开机自启"}
        if path.exists():
            path.unlink()
        return {"ok": True, "path": str(path), "hint": "已取消开机自启"}
    except OSError as exc:
        return {"ok": False, "path": str(path), "hint": f"改不动启动文件夹：{exc}"}
