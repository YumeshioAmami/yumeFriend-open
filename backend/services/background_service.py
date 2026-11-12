"""沉浸模式的背景图（v2.2 / D107）。

口径：
· 一个朋友一张，存 `user_data/backgrounds/<friend_id>.jpg`（**不建表**，跟立绘一个路子）
· 上传时后端**自动按 16:9 居中裁切 + 压到 1920 宽**（沉浸模式是全屏铺底，横构图才对）
· 删除要彻底：删朋友 / 删背景都把文件清掉（宪法第九条）
"""

from __future__ import annotations

import io
from pathlib import Path

from loguru import logger

from backend import config
from backend.services.errors import ServiceError

OUT_WIDTH = 1920
OUT_HEIGHT = 1080  # 16:9
MAX_UPLOAD_BYTES = 20 * 1024 * 1024


def file_of(friend_id: str) -> Path:
    return config.BACKGROUND_DIR / f"{friend_id}.jpg"


def path_for(friend_id: str) -> Path | None:
    path = file_of(friend_id)
    return path if path.is_file() else None


def url_of(friend_id: str) -> str | None:
    """给前端的地址（带 mtime 破缓存）；没有背景就是 None。"""
    path = path_for(friend_id)
    if path is None:
        return None
    try:
        stamp = int(path.stat().st_mtime)
    except OSError:
        stamp = 0
    return f"/api/friends/{friend_id}/background?v={stamp}"


def save(friend_id: str, raw: bytes) -> str:
    """存一张背景（自动 16:9 居中裁切）。返回相对 user_data 的路径。"""
    if not raw:
        raise ServiceError("BACKGROUND_EMPTY", "这张图是空的，换一张试试", 400)
    if len(raw) > MAX_UPLOAD_BYTES:
        raise ServiceError("BACKGROUND_TOO_BIG", "图太大了（上限 20MB），压一下再传", 400)
    try:
        from PIL import Image, ImageOps  # noqa: PLC0415

        with Image.open(io.BytesIO(raw)) as image:
            image = image.convert("RGB")
            fitted = ImageOps.fit(image, (OUT_WIDTH, OUT_HEIGHT), method=Image.LANCZOS)
            buffer = io.BytesIO()
            fitted.save(buffer, format="JPEG", quality=88, optimize=True)
    except ServiceError:
        raise
    except Exception as exc:  # noqa: BLE001 —— 坏图 / 不支持的格式都算"读不出来"
        raise ServiceError("BACKGROUND_BAD_IMAGE", f"这张图读不出来（{type(exc).__name__}）", 400) from exc

    path = file_of(friend_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(buffer.getvalue())
    logger.info("背景图已更新：{}（{} 字节）", friend_id[:8], path.stat().st_size)
    return f"backgrounds/{friend_id}.jpg"


def clear(friend_id: str) -> None:
    file_of(friend_id).unlink(missing_ok=True)
