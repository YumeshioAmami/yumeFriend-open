"""适配器注册表：`game_kind` → 启动命令（D250）。

主代码只认这一张表；加一个新游戏 = 加一行 + 一个目录，业务逻辑不动。
路径统一走 `RESOURCE_DIR`：开发时是仓库根，打包后是 `_internal/`（P4 / Q43）。
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

from backend.config import RESOURCE_DIR


def _node() -> str:
    name = "node.exe" if sys.platform == "win32" else "node"
    bundled = Path(RESOURCE_DIR) / "node" / name
    if bundled.is_file():
        return str(bundled)
    return shutil.which("node") or "node"


def command_for(game_kind: str) -> list[str]:
    """拿到某个游戏的适配器启动命令（参数由 `AdapterProcess` 追加）。"""
    kind = (game_kind or "").strip()
    if kind == "mock":
        return [
            sys.executable,
            str(Path(RESOURCE_DIR) / "adapters" / "mock" / "mock_adapter.py"),
        ]
    if kind == "minecraft":
        return [
            _node(),
            str(Path(RESOURCE_DIR) / "adapters" / "minecraft" / "index.js"),
        ]
    raise KeyError(f"没有这个游戏的适配器：{kind!r}")
