"""缩略图 / 压缩版本（v3.1）：**手机端拉图太慢**的解药。

问题（2026-09-22 实测）：手机上经过组网软件拉电脑的图，原图是
聊天图片 0.5~1.5 MB、立绘 ~288 KB/张（沉浸模式每换一个情绪就拉一张）。

所以：**按需生成压过的小图 + 磁盘缓存**（生成一次，之后直接发文件）。

口径：

· 只在传了 `w`（目标最长边）时才处理；**没传 / 处理失败 → 回原图**（宁可慢，不能白屏）
· 变体按 **原文件路径 + mtime + 目标尺寸** 缓存 —— 重传头像/立绘后自动失效，不会给旧的
· 统一输出 **webp q82**（有 alpha 就保留 alpha；浏览器、WebView、PIL 都认）
"""

from __future__ import annotations

import hashlib
from pathlib import Path

from loguru import logger

from backend import config

CACHE_DIR = config.USER_DATA_DIR / "thumbs"
QUALITY = 82


def variant(path: Path | None, max_side: int) -> Path | None:
    """要发出去的文件：`max_side<=0` 或处理失败 → 原图；否则是缓存里的压缩版。"""
    if path is None or max_side <= 0:
        return path
    try:
        stat = path.stat()
    except OSError:
        return path
    key = hashlib.sha1(
        f"{path}|{stat.st_mtime_ns}|{stat.st_size}|{max_side}".encode("utf-8")
    ).hexdigest()[:20]
    target = CACHE_DIR / f"{key}.webp"
    if target.is_file():
        return target
    try:
        from PIL import Image  # noqa: PLC0415

        with Image.open(path) as raw:
            image = raw.copy()
        if max(image.size) > max_side:
            image.thumbnail((max_side, max_side), Image.LANCZOS)
        if image.mode not in ("RGB", "RGBA"):
            image = image.convert("RGBA" if "A" in image.mode else "RGB")
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        tmp = target.with_name(target.name + ".tmp")
        image.save(tmp, format="WEBP", quality=QUALITY, method=4)
        tmp.replace(target)
        logger.debug("缩略图生成：{} → {}（{} KB）", path.name, target.name, target.stat().st_size // 1024)
        return target
    except Exception as exc:  # noqa: BLE001 —— 缩略图失败绝不能影响看图
        logger.warning("生成缩略图失败（回原图）：{}", exc)
        return path


def media_type_for(path: Path | None) -> str:
    """给 FileResponse 用的 media type（按扩展名，够用）。"""
    suffix = (path.suffix if path else "").lower()
    return {
        ".webp": "image/webp",
        ".png": "image/png",
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".gif": "image/gif",
    }.get(suffix, "application/octet-stream")


def cache_headers(path: Path | None, *, max_age: int = 86400) -> dict[str, str]:
    """**让手机把图缓存住**（跨刷新、跨次打开都不用重下）。"""
    etag = ""
    try:
        if path is not None:
            stat = path.stat()
            etag = f'"{stat.st_mtime_ns:x}-{stat.st_size:x}"'
    except OSError:
        pass
    headers = {"Cache-Control": f"public, max-age={max_age}"}
    if etag:
        headers["ETag"] = etag
    return headers
