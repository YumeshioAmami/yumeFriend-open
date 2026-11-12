"""用户发的图：接收 / 转正 / 取用 / 内联（v2.0，T2.0-6；策略见 D82）。

三个目录概念：

```
user_data/uploads/pending/<id>.<ext>   刚传上来、还没发出去（24 小时没人要就清）
user_data/images/<id>.<ext>            已经跟着消息发出去了（长期保留）
```

**为什么不做一张 uploads 表**：id 就是文件名，`转正` = 把文件挪个目录。
少一张表、少一处"表和文件不同步"的坑；取图时先查 images/ 再查 pending/。

安全：id 必须是 **32 位十六进制**（`uuid4().hex`）——既防路径穿越，也防乱猜别人传的图。
"""

from __future__ import annotations

import base64
import io
import os
import re
import uuid
from pathlib import Path

from loguru import logger
from PIL import Image, UnidentifiedImageError

from backend import config
from backend.services.errors import ServiceError

MAX_UPLOAD_BYTES = 10 * 1024 * 1024  # 10 MB（api.md 的【v2】1）
MAX_SIDE = 2048  # 最长边超过它 → 等比缩小（省 token、省带宽）
PENDING_TTL_HOURS = 24

_ID_RE = re.compile(r"^[0-9a-f]{32}$")
_FORMATS = {"PNG": "png", "JPEG": "jpg", "WEBP": "webp"}
_MEDIA_TYPES = {"png": "image/png", "jpg": "image/jpeg", "webp": "image/webp"}


def _media_type(ext: str) -> str:
    return _MEDIA_TYPES.get(ext.lower(), "application/octet-stream")


def _prepare(raw: bytes) -> tuple[bytes, str, int, int]:
    """校验 + 必要时缩小。返回 (字节, 扩展名, 宽, 高)。"""
    if not raw:
        raise ServiceError("IMAGE_EMPTY", "这张图是空的", 400)
    if len(raw) > MAX_UPLOAD_BYTES:
        raise ServiceError(
            "IMAGE_TOO_LARGE", f"图太大了（最大 {MAX_UPLOAD_BYTES // 1024 // 1024} MB）", 400
        )
    try:
        with Image.open(io.BytesIO(raw)) as image:
            fmt = (image.format or "").upper()
            if fmt not in _FORMATS:
                raise ServiceError("IMAGE_UNSUPPORTED", "只收 PNG / JPG / WEBP", 400)
            image.load()
            width, height = image.size
            if max(width, height) > MAX_SIDE:
                image.thumbnail((MAX_SIDE, MAX_SIDE), Image.LANCZOS)
                width, height = image.size
            if fmt == "JPEG" and image.mode not in ("RGB", "L"):
                image = image.convert("RGB")
            out = io.BytesIO()
            if fmt == "JPEG":
                image.save(out, format="JPEG", quality=85, optimize=True)
            else:
                image.save(out, format=fmt)
            return out.getvalue(), _FORMATS[fmt], width, height
    except ServiceError:
        raise
    except (UnidentifiedImageError, OSError, ValueError) as exc:
        raise ServiceError("IMAGE_UNSUPPORTED", "这看起来不是一张能打开的图片", 400) from exc


def save_upload(raw: bytes) -> dict:
    """收下一张图 → 落 `uploads/pending/`。返回给前端的元信息。"""
    payload, ext, width, height = _prepare(raw)
    image_id = uuid.uuid4().hex
    config.UPLOAD_PENDING_DIR.mkdir(parents=True, exist_ok=True)
    (config.UPLOAD_PENDING_DIR / f"{image_id}.{ext}").write_bytes(payload)
    return {
        "id": image_id,
        "url": f"/api/images/{image_id}",
        "width": width,
        "height": height,
        "bytes": len(payload),
    }


def save_capture(raw: bytes) -> dict:
    """截屏产物直接进 `images/`（它不是"待发送"的上传，是已经要用的图）。"""
    payload, ext, width, height = _prepare(raw)
    image_id = uuid.uuid4().hex
    config.IMAGE_DIR.mkdir(parents=True, exist_ok=True)
    (config.IMAGE_DIR / f"{image_id}.{ext}").write_bytes(payload)
    return {
        "id": image_id,
        "rel": f"images/{image_id}.{ext}",
        "width": width,
        "height": height,
        "bytes": len(payload),
    }


def path_for(image_id: str) -> Path | None:
    """按 id 找文件（先 images/ 再 pending/）。不合法 / 不存在都返回 None。"""
    if not _ID_RE.match(image_id or ""):
        return None
    for folder in (config.IMAGE_DIR, config.UPLOAD_PENDING_DIR):
        for suffix in _MEDIA_TYPES:
            candidate = folder / f"{image_id}.{suffix}"
            if candidate.is_file():
                return candidate
    return None


def media_type_for(path: Path) -> str:
    return _media_type(path.suffix.lstrip("."))


def promote(image_id: str) -> str | None:
    """把 pending 里的图**转正**成"已经发出去的图"。返回相对 `user_data` 的路径。

    - 已经在 images/ 里（重复发送同一条？）→ 直接用
    - 不存在 → None（调用方回 400）
    """
    if not _ID_RE.match(image_id or ""):
        return None
    for suffix in _MEDIA_TYPES:
        target = config.IMAGE_DIR / f"{image_id}.{suffix}"
        if target.is_file():
            return f"images/{target.name}"
    path = path_for(image_id)
    if path is None:
        return None
    config.IMAGE_DIR.mkdir(parents=True, exist_ok=True)
    target = config.IMAGE_DIR / path.name
    try:
        os.replace(path, target)  # 同一个盘 → 原子改名
    except OSError as exc:
        logger.warning("图片转正失败（{}）：{}", path, exc)
        return None
    return f"images/{target.name}"


def abs_path(rel_path: str | None) -> Path | None:
    """`messages.image_path`（相对 user_data）→ 真实文件；**挡住路径穿越**。"""
    if not rel_path:
        return None
    base = config.USER_DATA_DIR.resolve()
    path = (config.USER_DATA_DIR / rel_path).resolve()
    if path != base and base not in path.parents:
        return None
    return path if path.is_file() else None


def data_url(rel_path: str | None) -> str | None:
    """转成 `data:image/...;base64,...`——**送给模型只能这么塞**。

    为什么不用我们自己的 `/api/images/<id>` 地址：那是 `127.0.0.1` 上的，
    云端模型根本访问不到（D82）。
    """
    path = abs_path(rel_path)
    if path is None:
        return None
    try:
        payload = base64.b64encode(path.read_bytes()).decode("ascii")
    except OSError as exc:
        logger.warning("读图失败（{}）：{}", path, exc)
        return None
    return f"data:{media_type_for(path)};base64,{payload}"


def cleanup_pending(*, older_than_hours: int = PENDING_TTL_HOURS, now: float | None = None) -> int:
    """删掉"传了但一直没发出去"的图（architecture.md §8 的清理任务会调它）。"""
    import time

    if not config.UPLOAD_PENDING_DIR.is_dir():
        return 0
    deadline = (now if now is not None else time.time()) - older_than_hours * 3600
    removed = 0
    for path in config.UPLOAD_PENDING_DIR.iterdir():
        try:
            if path.is_file() and path.stat().st_mtime < deadline:
                path.unlink()
                removed += 1
        except OSError:  # noqa: PERF203 —— 单个文件删不掉不该影响其它
            continue
    return removed


# ---------- 图片清理（v3.0 / D141）----------


def remove_for_friend(session, friend_id: str) -> int:  # noqa: ANN001 —— Session（避免循环 import）
    """删掉某个朋友发过的**全部**图片文件。给他"彻底删除"用（宪法§9）。返回文件数。

    ⚠️ 要在**删消息行之前**调 —— 行没了就查不出该删哪些文件了。
    """
    from sqlalchemy import select  # noqa: PLC0415

    from backend.models import Message  # noqa: PLC0415 —— 懒导入，避免 services ↔ models 打转

    rels = list(
        session.execute(
            select(Message.image_path).where(
                Message.friend_id == friend_id, Message.image_path.is_not(None)
            )
        ).scalars()
    )
    removed = 0
    for rel in rels:
        path = abs_path(str(rel))
        if path is None:
            continue
        try:
            path.unlink()
            removed += 1
        except OSError as exc:  # noqa: PERF203
            logger.warning("图片删不掉（忽略）：{}", exc)
    return removed


def purge_orphans(session) -> int:  # noqa: ANN001 —— Session
    """删掉**没有任何消息引用**的图片文件（孤儿）——每天兜底扫一次（宪法§9 不留 orphan）。

    会出现的场景：删过朋友（消息级联没了、文件还在）、或者手工清过库。
    """
    from sqlalchemy import select  # noqa: PLC0415

    from backend.models import Message  # noqa: PLC0415

    if not config.IMAGE_DIR.is_dir():
        return 0
    if not config.cleanup_targets_are_consistent():
        logger.warning("库和图片目录不在同一个 user_data 里，本轮**拒绝清理**（防止误删）")
        return 0
    # 库里存的就是相对 user_data 的路径（`images/<id>.png`），**别再加前缀**（加过，白删过一轮）
    known = {
        str(name)
        for name in session.execute(
            select(Message.image_path).where(Message.image_path.is_not(None))
        ).scalars()
        if name
    }
    removed = 0
    for path in config.IMAGE_DIR.iterdir():
        if not path.is_file():
            continue
        if f"images/{path.name}" in known:
            continue
        try:
            path.unlink()
            removed += 1
        except OSError:  # noqa: PERF203
            continue
    if removed:
        logger.info("清理孤儿图片：{} 张", removed)
    return removed
