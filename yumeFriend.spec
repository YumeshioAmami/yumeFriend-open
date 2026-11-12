# -*- mode: python ; coding: utf-8 -*-
"""yumeFriend 打包 spec（v3.0 / T3.0-A2）。

用法（在仓库根）：
    .\\.venv\\Scripts\\python.exe -m PyInstaller --noconfirm yumeFriend.spec
产物：
    dist/yumeFriend/yumeFriend.exe   ← 目录模式（启动快、出问题好查），发给别人整个文件夹

**只读资源**打进包（前端 dist / alembic / migrations / assets / 语音小助手脚本），
**可写数据**由 `backend/config.py` 决定放 **exe 旁边的 user_data/**（绿色版，用户拍板）。
"""

from pathlib import Path
import shutil

from PyInstaller.utils.hooks import collect_all

ROOT = Path(SPECPATH)  # noqa: F821 —— PyInstaller 注入的全局变量

# v3.46（D231）：本地嵌入（fastembed → onnxruntime / tokenizers）带原生 DLL 与数据，
# 静态分析收不全 → 显式 collect_all 进来（不然打包版里"向量检索"一跑就炸 module not found）。
_embed_datas: list = []
_embed_binaries: list = []
_embed_hidden: list = []
for _pkg in ("fastembed", "onnxruntime", "tokenizers"):
    _d, _b, _h = collect_all(_pkg)
    _embed_datas += _d
    _embed_binaries += _b
    _embed_hidden += _h

# D424：桌宠 Live2D 带原生扩展、GLFW DLL 和 shader 数据，静态分析收不全。
_live2d_datas: list = []
_live2d_binaries: list = []
_live2d_hidden: list = []
for _pkg in ("live2d", "glfw"):
    _d, _b, _h = collect_all(_pkg)
    _live2d_datas += _d
    _live2d_binaries += _b
    _live2d_hidden += _h

# v5.0（P4 / Q43 / D477）：游戏适配器 + Node 运行时（绿色版自带依赖）
#   adapters/ 整个目录进包；minecraft-data 只留共享协议代码，版本数据不进包，
#   游戏启动前按服务器版本从资源站下载到 user_data/minecraft/versions/。
#   node.exe 从构建机的 PATH 上取（~81 MB）→ _internal/node/node.exe
_node = shutil.which("node")
if not _node:
    raise SystemExit("打包需要 Node（Q43）：先装 Node 或把它放进 PATH")
_binaries = list(_embed_binaries) + _live2d_binaries
_binaries.append((_node, "node"))

datas = [
    # 前端构建产物（后端在 / 上挂它，见 backend/app.py）
    (str(ROOT / "frontend" / "dist"), "frontend/dist"),
    # 迁移：alembic.ini 里写的是 %(here)s/backend/migrations → 两个得一起、还得是这个相对形状
    (str(ROOT / "alembic.ini"), "."),
    (str(ROOT / "backend" / "migrations"), "backend/migrations"),
    # 语音识别的小助手（被 GPT-SoVITS 的 python 执行，不是 import，所以当数据带上）
    (str(ROOT / "backend" / "speech"), "backend/speech"),
    # 图标 / logo（托盘、窗口、任务栏）
    (str(ROOT / "assets"), "assets"),
    # v5.0（P4 / Q43）：游戏适配器（Node + 裁剪后的 minecraft-data）
    (str(ROOT / "adapters"), "adapters"),
] + _embed_datas + _live2d_datas

# 这些是"用得到但静态分析看不出来"的（uvicorn 的协议/循环、webview 的 winforms 后端…）
hiddenimports = [
    # 后端 ASGI app（desktop/app.py 里现在是显式 import，这里再保一道）
    "backend.app",
    "uvicorn.logging",
    "uvicorn.loops",
    "uvicorn.loops.auto",
    "uvicorn.protocols",
    "uvicorn.protocols.http",
    "uvicorn.protocols.http.auto",
    "uvicorn.protocols.websockets",
    "uvicorn.protocols.websockets.auto",
    "uvicorn.lifespan",
    "uvicorn.lifespan.on",
    "sqlalchemy.dialects.sqlite",
    "alembic.ddl.sqlite",
    "apscheduler.triggers.cron",
    "apscheduler.triggers.interval",
    "apscheduler.executors.asyncio",
    "webview.platforms.winforms",
    "pystray._win32",
    "pypdf",
    "docx",
    "PIL._tkinter_finder",  # 常见坑：PIL 会尝试 import 它，不加会 warning/报错
    # v3.46（D231）：本地嵌入
    "fastembed",
    "onnxruntime",
    "tokenizers",
] + _embed_hidden + _live2d_hidden

a = Analysis(  # noqa: F821
    [str(ROOT / "scripts" / "packaged_entry.py")],
    pathex=[str(ROOT)],
    binaries=_binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    # v3.46（D231）：**numpy 不能再排除了**（onnxruntime / fastembed 要用它）。
    # tkinter 我们不用、matplotlib 没有、pytest 只跟开发有关。
    excludes=["tkinter", "matplotlib", "pytest", "_pytest"],
    noarchive=False,
)
pyz = PYZ(a.pure)  # noqa: F821

exe = EXE(  # noqa: F821
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="yumeFriend",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,  # 双击不弹黑窗口（日志在 user_data/logs 里）
    disable_windowed_traceback=False,
    icon=str(ROOT / "assets" / "yumeFriend.ico"),
)

coll = COLLECT(  # noqa: F821
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="yumeFriend",
)
