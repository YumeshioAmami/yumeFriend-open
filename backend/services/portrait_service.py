"""立绘库（v2.2 / T2.2-3 / D102）：**对齐 LingChat 的目录与命名**，导入即用。

约定（就是 LingChat 那套）：

```
一个朋友一个目录：user_data/portraits/<friend_id>/
文件名（去后缀）就是情绪：正常.webp  高兴.webp  害羞.png  …（中文名原样，不做映射）
头像.webp  → 不是立绘，是"顺手当头像用"
```

**不建表**（D102）：情绪就是文件名；"有没有立绘"= 目录里有没有文件。
"""

from __future__ import annotations

import io
import re
import shutil
from datetime import datetime, timezone
from pathlib import Path

from loguru import logger
from PIL import Image, UnidentifiedImageError
from sqlalchemy.orm import Session

from backend import config
from backend.models import Friend
from backend.services import avatar_service
from backend.services.errors import NotFoundError, ServiceError

MAX_SIDE = 2048
IMAGE_SUFFIXES = {".png": "png", ".webp": "webp", ".jpg": "jpg", ".jpeg": "jpg"}
AVATAR_STEM = "头像"
_SAFE_EMOTION = re.compile(r"^[\w\u4e00-\u9fff·\-（）()\s]{1,24}$")
DEFAULT_PORTRAIT = config.RESOURCE_DIR / "assets" / "defaults" / "portrait.png"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def dir_of(friend_id: str, pack_id: str | None = None) -> Path:
    if pack_id:
        from backend.services import portrait_pack_service  # 懒导入避免循环

        return portrait_pack_service.pack_dir(pack_id)
    return config.PORTRAIT_DIR / friend_id


def _prepare(raw: bytes) -> tuple[bytes, str, tuple[int, int]]:
    """校验是不是能打开的图；太大就等比缩小（跟图片那套一致）。"""
    try:
        with Image.open(io.BytesIO(raw)) as image:
            image.load()
            width, height = image.size
            fmt = (image.format or "PNG").upper()
            if max(width, height) > MAX_SIDE:
                image.thumbnail((MAX_SIDE, MAX_SIDE), Image.LANCZOS)
                width, height = image.size
            if fmt == "JPEG":
                out = io.BytesIO()
                image.convert("RGB").save(out, format="JPEG", quality=88, optimize=True)
                return out.getvalue(), "jpg", (width, height)
            if fmt == "WEBP":
                out = io.BytesIO()
                image.save(out, format="WEBP", quality=90, method=4)
                return out.getvalue(), "webp", (width, height)
            out = io.BytesIO()
            image.save(out, format="PNG")
            return out.getvalue(), "png", (width, height)
    except (UnidentifiedImageError, OSError, ValueError) as exc:
        raise ServiceError("PORTRAIT_UNSUPPORTED", "这看起来不是一张能打开的图片", 400) from exc


def _has_images(path: Path) -> bool:
    return path.is_dir() and any(
        child.is_file() and child.suffix.lower() in IMAGE_SUFFIXES for child in path.iterdir()
    )


def ensure_default(friend_id: str) -> Path | None:
    """没有自定义立绘的朋友，也有一张默认「正常」立绘（D424）。

    这张图是所有朋友共用的系统默认，不占用某位朋友的真实情绪库；
    用户导入任何立绘后，文件自然覆盖/并列，不需要再走空态。
    """
    folder = dir_of(friend_id)
    if _has_images(folder):
        return None
    if not DEFAULT_PORTRAIT.is_file():
        return None
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / "正常.png"
    if not target.is_file():
        shutil.copyfile(DEFAULT_PORTRAIT, target)
    return target


def ensure_all_defaults(friend_ids: list[str]) -> int:
    """启动时补齐旧朋友的默认立绘；返回这次新补了几个。"""
    created = 0
    for friend_id in friend_ids:
        try:
            if ensure_default(friend_id) is not None:
                created += 1
        except OSError:
            logger.warning("默认立绘补齐失败（{}）", friend_id)
    return created


def resolve_dir(source_dir: str) -> Path:
    """把用户填的目录"落到真正放着立绘的那一层"（D108）。

    实测踩过：用户填的是角色文件夹（`…/characters/DeepSeek`），而立绘躺在
    `…/DeepSeek/avatar/` 里 —— 原来直接扫这一层，一张都认不到，用户只看到"没扫上"。
    现在的规矩：填的这一层有图就用它；没有就往下找一层（优先 `avatar/`，
    再挑任何一个有图的子目录）。还找不到就原样返回，让调用方给人话。
    """
    path = Path((source_dir or "").strip().strip('"'))
    if not path.is_dir():
        raise ServiceError("PORTRAIT_DIR_NOT_FOUND", f"这个目录不存在：{path}", 400)
    if _has_images(path):
        return path
    preferred = path / "avatar"
    if _has_images(preferred):
        return preferred
    for child in sorted(path.iterdir()):
        if child.is_dir() and _has_images(child):
            return child
    return path


def scan(source_dir: str) -> list[dict]:
    """扫一个目录，列出能导入的立绘。**不落盘。**"""
    path = resolve_dir(source_dir)
    found: dict[str, dict] = {}
    for child in sorted(path.iterdir()):
        if not child.is_file() or child.suffix.lower() not in IMAGE_SUFFIXES:
            continue
        emotion = child.stem.strip()
        if not emotion or emotion == AVATAR_STEM or not _SAFE_EMOTION.match(emotion):
            continue
        if emotion in found:  # 同名不同后缀：先到先得，记一笔
            found[emotion]["duplicated"] = True
            continue
        found[emotion] = {
            "emotion": emotion,
            "path": str(child),
            "suffix": child.suffix.lower(),
            "bytes": child.stat().st_size,
            "duplicated": False,
        }
    return list(found.values())


def _find_avatar_file(source_dir: str) -> Path | None:
    try:
        path = resolve_dir(source_dir)
    except ServiceError:
        return None
    for child in sorted(path.iterdir()):
        if child.is_file() and child.stem.strip() == AVATAR_STEM and child.suffix.lower() in IMAGE_SUFFIXES:
            return child
    return None


def import_from(
    session: Session,
    friend: Friend,
    *,
    source_dir: str,
    emotions: list[str] | None = None,
    use_avatar: bool = False,
) -> dict:
    """把立绘拷进 `user_data/portraits/<friend_id>/`（**情绪 = 文件名**）。"""
    candidates = scan(source_dir)
    if not candidates:
        raise ServiceError("PORTRAIT_NOTHING_FOUND", "这个目录里没认出立绘（文件名当情绪，比如 高兴.png）", 400)
    wanted = {item.strip() for item in emotions} if emotions else None
    target = dir_of(friend.id)
    target.mkdir(parents=True, exist_ok=True)

    imported: list[str] = []
    for item in candidates:
        if wanted is not None and item["emotion"] not in wanted:
            continue
        source = Path(item["path"])
        payload, ext, _size = _prepare(source.read_bytes())
        # 同名情绪先清掉旧后缀（换格式导入时别留两份）
        for old in target.glob(f"{item['emotion']}.*"):
            old.unlink(missing_ok=True)
        (target / f"{item['emotion']}.{ext}").write_bytes(payload)
        imported.append(item["emotion"])

    avatar_set = False
    if use_avatar:
        avatar_file = _find_avatar_file(source_dir)
        if avatar_file is not None:
            try:
                avatar_service.save(friend, avatar_file.read_bytes())
                avatar_set = True
            except Exception as exc:  # noqa: BLE001 —— 头像失败不影响立绘导入
                logger.warning("顺手设头像失败（不影响立绘）：{}", exc)

    logger.info("导入立绘：{} → {} 张（{}）", friend.name, len(imported), "、".join(imported[:6]))
    return {"imported": imported, "avatar_set": avatar_set, "portraits": list_portraits(friend.id)}


def save_bytes(
    friend_id: str,
    emotion: str,
    raw: bytes,
    pack_id: str | None = None,
) -> dict:
    """手机批量导入：原始图片 → 服务端统一压缩 → 按情绪覆盖。"""
    if pack_id:
        from backend.services import portrait_pack_service

        if not portrait_pack_service.is_legacy_pack(pack_id):
            raise ServiceError(
                "PORTRAIT_PACK_GLOBAL_READONLY",
                "全局立绘包请在电脑端「设置 → 立绘包」里管理",
                400,
            )
        return portrait_pack_service.save_bytes(pack_id, emotion, raw)
    name = (emotion or "").strip()
    if not _SAFE_EMOTION.match(name) or "/" in name or "\\" in name or ".." in name:
        raise ValueError(f"情绪名不合法：{name!r}")
    payload, ext, size = _prepare(raw)
    target = dir_of(friend_id)
    target.mkdir(parents=True, exist_ok=True)
    for old in target.glob(f"{name}.*"):
        old.unlink(missing_ok=True)
    path = target / f"{name}.{ext}"
    path.write_bytes(payload)
    return {"emotion": name, "size": size, "path": str(path)}


def list_portraits(friend_id: str, pack_id: str | None = None) -> list[dict]:
    """现有立绘（情绪 + 尺寸 + 改过的文件更新了就带个破缓存的版本号）。"""
    folder = dir_of(friend_id, pack_id)
    if not folder.is_dir():
        return []
    items: list[dict] = []
    for child in sorted(folder.iterdir()):
        if not child.is_file() or child.suffix.lower() not in IMAGE_SUFFIXES:
            continue
        size: list[int] = []
        try:
            with Image.open(child) as image:
                size = [image.size[0], image.size[1]]
        except Exception:  # noqa: BLE001 —— 坏图也列出来，用户能删
            size = []
        items.append(
            {
                "emotion": child.stem.strip(),
                "suffix": child.suffix.lower(),
                "bytes": child.stat().st_size,
                "size": size,
                "updated_at": datetime.fromtimestamp(child.stat().st_mtime).isoformat(timespec="seconds"),
            }
        )
    return items


def emotions(friend_id: str, pack_id: str | None = None) -> list[str]:
    """给她看的"你能用的表情"（**只列真有图的**）。"""
    return [item["emotion"] for item in list_portraits(friend_id, pack_id)]


def file_for(friend_id: str, emotion: str, pack_id: str | None = None) -> Path | None:
    """按情绪取文件（**挡路径穿越**：情绪名必须干净）。"""
    name = (emotion or "").strip()
    if not _SAFE_EMOTION.match(name) or "/" in name or "\\" in name or ".." in name:
        return None
    folder = dir_of(friend_id, pack_id)
    for suffix in IMAGE_SUFFIXES:
        candidate = folder / f"{name}{suffix}"
        if candidate.is_file():
            return candidate
    return None


def delete(friend_id: str, emotion: str, pack_id: str | None = None) -> None:
    path = file_for(friend_id, emotion, pack_id)
    if path is None:
        raise NotFoundError("PORTRAIT_NOT_FOUND", "没有这个表情的立绘")
    path.unlink(missing_ok=True)


def clear_all(friend_id: str) -> None:
    """删朋友时把整个立绘目录清掉（删除要彻底，宪法第九条）。"""
    folder = dir_of(friend_id)
    if folder.is_dir():
        shutil.rmtree(folder, ignore_errors=True)
