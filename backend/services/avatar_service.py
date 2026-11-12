"""头像（v2，T2.0-3）：上传时**统一处理**成 256×256 的 PNG。

为什么在服务端处理：手机/相机随手一张就是 4000×3000、好几 MB——
不处理的话，聊天界面每次渲染都要传大图（费流量、费内存、还慢）。
这里统一：**居中裁成正方形 → 缩到 256×256 → 存 PNG**。

文件落在 `user_data/avatars/<friend_id>.png`，数据库里只存**相对路径**。
"""

from __future__ import annotations

import io
from pathlib import Path

from loguru import logger
from PIL import Image, UnidentifiedImageError

from backend import config
from backend.models import Friend
from backend.services.errors import ServiceError

SIZE = 256
MAX_BYTES = 10 * 1024 * 1024  # 10 MB：比这大的一律拒掉


def file_path(friend: Friend) -> Path | None:
    """头像文件的绝对路径；没有 / 文件丢了就返回 None（界面回退色块）。"""
    if not friend.avatar_path:
        return _fallback_path()
    # 只取文件名：数据库里就算被人塞了 "../" 也出不去
    # 注意：**每次读 config.AVATAR_DIR**，别在模块顶部 import 成常量
    # （那样测试里 monkeypatch 就不生效了，文件会偷偷写进真实目录 —— 踩过）
    path = config.AVATAR_DIR / Path(friend.avatar_path).name
    return path if path.exists() else _fallback_path()


def _fallback_path() -> Path | None:
    """所有朋友共享的默认立绘/头像；用户上传后覆盖这个回退。"""
    fallback = config.AVATAR_DIR / "default.png"
    return fallback if fallback.is_file() else None


def save(friend: Friend, raw: bytes) -> None:
    """校验 + 裁剪 + 落盘 + 写字段。失败一律抛 ServiceError（API 层映射成 4xx）。"""
    if not raw:
        raise ServiceError("AVATAR_EMPTY", "没收到图片内容")
    if len(raw) > MAX_BYTES:
        raise ServiceError("AVATAR_TOO_LARGE", "图片太大了（上限 10 MB）")
    try:
        with Image.open(io.BytesIO(raw)) as image:
            image = image.convert("RGB")
            width, height = image.size
            side = min(width, height)
            left, top = (width - side) // 2, (height - side) // 2
            cropped = image.crop((left, top, left + side, top + side))
            resized = cropped.resize((SIZE, SIZE), Image.LANCZOS)
            config.AVATAR_DIR.mkdir(parents=True, exist_ok=True)
            # 文件名固定成 <id>.png：换头像直接覆盖，URL 上的 ?v= 负责破缓存
            resized.save(config.AVATAR_DIR / f"{friend.id}.png", format="PNG", optimize=True)
    except UnidentifiedImageError as exc:
        raise ServiceError("AVATAR_NOT_IMAGE", "这不是一张能识别的图片") from exc
    except OSError as exc:  # 坏文件 / 磁盘问题
        raise ServiceError("AVATAR_BROKEN", f"这张图读不了：{exc}") from exc

    friend.avatar_path = f"avatars/{friend.id}.png"
    logger.info("头像已更新：{}", friend.name)


def clear(friend: Friend) -> None:
    """删掉头像文件并清字段（删除朋友 / 用户主动删头像都走这里）。"""
    # 只删这个朋友自己的头像；别把共享的 default.png 当成她的文件删掉。
    path = (
        config.AVATAR_DIR / Path(friend.avatar_path).name
        if friend.avatar_path
        else None
    )
    if path is not None:
        try:
            path.unlink()
        except OSError:  # noqa: PERF203 —— 删不掉也不能把整个删除流程搞挂
            logger.warning("头像文件删不掉（忽略）：{}", path)
    friend.avatar_path = None
