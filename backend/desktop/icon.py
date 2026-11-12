"""yumeFriend 的图标（D127→D132）：**一处画，四处用**。

logo = 用户给的那张图（**猫耳弯月 + 暖橙渐变**，梦 × 朋友）。
处理过的成品就在仓库里：`assets/yumeFriend-large.png`（512，透明圆角、已去水印）。

这个模块只做一件事：**把那张成品按需要的尺寸读出来**，给托盘 / 窗口用。
（原始 2048 大图 + 去水印 + 抠透明的脚本在 `scripts/make_logo.py`，换图时跑它。）
"""

from __future__ import annotations

from pathlib import Path

from PIL import Image

from backend.config import RESOURCE_DIR

# 成品图（512）：前端 LogoMark / favicon / assets png+ico 也都是它生成的。
# 打包后从解包目录找（`RESOURCE_DIR`），开发时 = 仓库根。
_SOURCE = Path(RESOURCE_DIR) / "assets" / "yumeFriend-large.png"


def app_icon(size: int = 256) -> Image.Image:
    """读成品图并缩到 `size`（透明圆角已经做好了）。读不到就给一张纯色兜底，不炸。"""
    try:
        image = Image.open(_SOURCE).convert("RGBA")
        return image.resize((size, size), Image.LANCZOS)
    except Exception:  # noqa: BLE001 —— 图标丢了也不该影响启动
        return Image.new("RGBA", (size, size), (176, 85, 47, 255))
