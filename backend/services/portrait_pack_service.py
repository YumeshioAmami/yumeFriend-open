"""全局立绘包（v7.43 / D441）。

立绘从“每个朋友一个目录”改成“全局立绘包 + 朋友选择包”。旧目录不搬：
`user_data/portraits/<friend_id>/` 会以 `legacy_<friend_id>` 的形式出现在列表里，
朋友没选新包时继续用旧目录。
"""

from __future__ import annotations

import re
import shutil
from datetime import datetime
from pathlib import Path
from typing import Iterable

from PIL import Image

from backend import config
from backend.services.errors import ServiceError

IMAGE_SUFFIXES = {".png": "png", ".webp": "webp", ".jpg": "jpg", ".jpeg": "jpg"}
_SAFE_EMOTION = re.compile(r"^[\w\u4e00-\u9fff·\-（）()\s]{1,24}$")
_SAFE_PACK = re.compile(r"^[\w\u4e00-\u9fff·\-()（）\s]{1,40}$")
LEGACY_PREFIX = "legacy_"


def _portrait_service():
    from backend.services import portrait_service

    return portrait_service


def is_legacy_pack(pack_id: str) -> bool:
    return str(pack_id or "").startswith(LEGACY_PREFIX)


def legacy_pack_id(friend_id: str) -> str:
    return f"{LEGACY_PREFIX}{friend_id}"


def pack_id_for_friend(friend_id: str, selected_pack_id: str | None) -> str | None:
    if selected_pack_id:
        return selected_pack_id
    folder = config.PORTRAIT_DIR / friend_id
    if _has_images(folder):
        return legacy_pack_id(friend_id)
    return None


def pack_dir(pack_id: str) -> Path:
    text = str(pack_id or "").strip()
    if is_legacy_pack(text):
        friend_id = text[len(LEGACY_PREFIX) :]
        if not friend_id or "/" in friend_id or "\\" in friend_id or ".." in friend_id:
            raise ServiceError("PORTRAIT_PACK_INVALID", "立绘包路径不合法", 400)
        return config.PORTRAIT_DIR / friend_id
    if not _SAFE_PACK.match(text) or text in {".", ".."}:
        raise ServiceError("PORTRAIT_PACK_INVALID", "立绘包名不合法", 400)
    return config.PORTRAIT_PACK_DIR / text


def _has_images(folder: Path) -> bool:
    return folder.is_dir() and any(
        item.is_file() and item.suffix.lower() in IMAGE_SUFFIXES for item in folder.iterdir()
    )


def _image_items(pack_id: str) -> list[dict]:
    folder = pack_dir(pack_id)
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
                "updated_at": datetime.fromtimestamp(child.stat().st_mtime).isoformat(
                    timespec="seconds"
                ),
            }
        )
    return items


def list_packs(friend_names: dict[str, str] | None = None) -> list[dict]:
    friend_names = friend_names or {}
    packs: list[dict] = []
    seen: set[str] = set()
    # 新全局包
    if config.PORTRAIT_PACK_DIR.is_dir():
        for folder in sorted(config.PORTRAIT_PACK_DIR.iterdir()):
            if not folder.is_dir() or not _has_images(folder):
                continue
            pack_id = folder.name
            items = _image_items(pack_id)
            packs.append(
                {
                    "id": pack_id,
                    "name": folder.name,
                    "legacy": False,
                    "friend_id": None,
                    "emotions": len(items),
                    "bytes": sum(int(item["bytes"]) for item in items),
                    "items": items,
                }
            )
            seen.add(pack_id)
    # 旧的朋友目录也列出来，不搬文件
    if config.PORTRAIT_DIR.is_dir():
        for folder in sorted(config.PORTRAIT_DIR.iterdir()):
            if not folder.is_dir() or not _has_images(folder):
                continue
            friend_id = folder.name
            pack_id = legacy_pack_id(friend_id)
            if pack_id in seen:
                continue
            items = _image_items(pack_id)
            name = friend_names.get(friend_id) or friend_id[:8]
            packs.append(
                {
                    "id": pack_id,
                    "name": f"旧版·{name}",
                    "legacy": True,
                    "friend_id": friend_id,
                    "emotions": len(items),
                    "bytes": sum(int(item["bytes"]) for item in items),
                    "items": items,
                }
            )
    return packs


def get_pack(pack_id: str) -> dict | None:
    for pack in list_packs():
        if pack["id"] == pack_id:
            return pack
    return None


def _unique_pack_id(name: str) -> str:
    base = str(name or "").strip()
    if not _SAFE_PACK.match(base):
        raise ServiceError("PORTRAIT_PACK_INVALID", "立绘包名只能用中文/字母/数字/短横线", 400)
    candidate = base
    index = 2
    while pack_dir(candidate).exists():
        candidate = f"{base}-{index}"
        index += 1
    return candidate


def create_from_dir(name: str, source_dir: str) -> dict:
    portrait_service = _portrait_service()
    candidates = portrait_service.scan(source_dir)
    if not candidates:
        raise ServiceError(
            "PORTRAIT_PACK_NOTHING_FOUND",
            "这个目录里没认出立绘（文件名当情绪，比如 高兴.png）",
            400,
        )
    pack_id = _unique_pack_id(name)
    target = pack_dir(pack_id)
    target.mkdir(parents=True, exist_ok=False)
    imported: list[str] = []
    for item in candidates:
        raw = Path(item["path"]).read_bytes()
        payload, ext, _size = portrait_service._prepare(raw)
        (target / f"{item['emotion']}.{ext}").write_bytes(payload)
        imported.append(item["emotion"])
    pack = get_pack(pack_id)
    return {"pack": pack, "imported": imported}


def save_bytes(pack_id: str, emotion: str, raw: bytes) -> dict:
    portrait_service = _portrait_service()
    name = str(emotion or "").strip()
    if not _SAFE_EMOTION.match(name) or "/" in name or "\\" in name or ".." in name:
        raise ServiceError("PORTRAIT_EMOTION_INVALID", f"情绪名不合法：{name!r}", 400)
    target = pack_dir(pack_id)
    target.mkdir(parents=True, exist_ok=True)
    payload, ext, size = portrait_service._prepare(raw)
    for old in target.glob(f"{name}.*"):
        old.unlink(missing_ok=True)
    path = target / f"{name}.{ext}"
    path.write_bytes(payload)
    return {"emotion": name, "size": size, "path": str(path)}


def file_for(pack_id: str, emotion: str) -> Path | None:
    name = str(emotion or "").strip()
    if not _SAFE_EMOTION.match(name) or "/" in name or "\\" in name or ".." in name:
        return None
    folder = pack_dir(pack_id)
    for suffix in IMAGE_SUFFIXES:
        candidate = folder / f"{name}{suffix}"
        if candidate.is_file():
            return candidate
    return None


def delete_pack(pack_id: str) -> None:
    if is_legacy_pack(pack_id):
        raise ServiceError(
            "PORTRAIT_PACK_LEGACY",
            "旧版朋友立绘目录不能在这里删；先把朋友换到别的立绘包",
            400,
        )
    target = pack_dir(pack_id)
    root = config.PORTRAIT_PACK_DIR.resolve()
    resolved = target.resolve()
    if root not in resolved.parents:
        raise ServiceError("PORTRAIT_PACK_INVALID", "立绘包路径越界", 400)
    if target.is_dir():
        shutil.rmtree(target)


def delete_emotion(pack_id: str, emotion: str) -> None:
    path = file_for(pack_id, emotion)
    if path is not None:
        path.unlink(missing_ok=True)
