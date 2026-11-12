"""Live2D 模型注册表（D440）。

模型本体可以散落在默认资源目录和 `user_data/live2d/` 下。设置必须按**模型**共享，
所以这里用 moc3 内容的 SHA-256 指纹做稳定 ID：同一份模型复制到不同朋友目录，
也只会得到一行设置。
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from backend import config

RUNTIME_JSON = "_yumefriend.model3.json"


@dataclass(frozen=True)
class Live2DModelInfo:
    id: str
    name: str
    model_file: Path
    source: str
    moc_file: Path


def _is_runtime_file(path: Path) -> bool:
    return path.name == RUNTIME_JSON or path.name.startswith("_yumefriend.")


def _moc_file(model_file: Path) -> Path | None:
    try:
        data = json.loads(model_file.read_text(encoding="utf-8-sig"))
    except Exception:  # noqa: BLE001 —— 坏模型不参与列表
        return None
    moc = str((data.get("FileReferences") or {}).get("Moc") or "").strip()
    if not moc:
        return None
    path = (model_file.parent / moc).resolve()
    if not path.is_file():
        return None
    return path


@lru_cache(maxsize=128)
def _fingerprint_cached(path_text: str, mtime_ns: int, size: int) -> str:
    del mtime_ns, size  # 只用来让 lru_cache 在文件变化时失效
    digest = hashlib.sha256()
    with open(path_text, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()[:16]


def model_id_for_file(model_file: Path) -> str | None:
    moc = _moc_file(model_file)
    if moc is None:
        return None
    stat = moc.stat()
    return _fingerprint_cached(str(moc), stat.st_mtime_ns, stat.st_size)


def model_info_for_file(model_file: Path, *, source: str = "") -> Live2DModelInfo | None:
    model_file = Path(model_file).resolve()
    moc = _moc_file(model_file)
    if moc is None:
        return None
    model_id = model_id_for_file(model_file)
    if not model_id:
        return None
    parent_name = model_file.parent.name or model_file.stem
    model_stem = re.sub(r"\.model3$", "", model_file.stem, flags=re.IGNORECASE)
    name = (
        model_stem
        if re.fullmatch(r"[0-9a-fA-F-]{20,}", parent_name)
        else parent_name
    )
    return Live2DModelInfo(
        id=model_id,
        name=name,
        model_file=model_file,
        source=source,
        moc_file=moc,
    )


def _append_models(
    folder: Path,
    *,
    source: str,
    found: list[Live2DModelInfo],
    seen_paths: set[str],
) -> None:
    if not folder.is_dir():
        return
    for path in sorted(folder.rglob("*.model3.json")):
        if _is_runtime_file(path):
            continue
        resolved = path.resolve()
        key = str(resolved)
        if key in seen_paths:
            continue
        info = model_info_for_file(resolved, source=source)
        if info is None:
            continue
        found.append(info)
        seen_paths.add(key)


def discover_models() -> list[Live2DModelInfo]:
    """默认模型优先，随后扫描仓库内置和 user_data/live2d 下的模型，按指纹去重。"""
    found: list[Live2DModelInfo] = []
    seen_paths: set[str] = set()

    _append_models(
        config.DEFAULT_LIVE2D_DIR,
        source="default",
        found=found,
        seen_paths=seen_paths,
    )

    assets_root = config.LIVE2D_ASSETS_DIR
    default_root = config.DEFAULT_LIVE2D_DIR.resolve()
    if assets_root.is_dir():
        for folder in sorted(assets_root.iterdir()):
            if not folder.is_dir() or folder.resolve() == default_root:
                continue
            _append_models(
                folder,
                source="bundled",
                found=found,
                seen_paths=seen_paths,
            )

    base = config.LIVE2D_DIR
    if base.is_dir():
        for path in sorted(base.rglob("*.model3.json")):
            if _is_runtime_file(path):
                continue
            resolved = path.resolve()
            if str(resolved) in seen_paths:
                continue
            source = "friend" if "friends" in resolved.parts else "user"
            info = model_info_for_file(resolved, source=source)
            if info is None:
                continue
            found.append(info)
            seen_paths.add(str(resolved))

    # 同一份模型复制成多个目录时，展示第一次出现的那份。
    unique: dict[str, Live2DModelInfo] = {}
    for info in found:
        unique.setdefault(info.id, info)
    return list(unique.values())


def get_model(model_id: str) -> Live2DModelInfo | None:
    for info in discover_models():
        if info.id == model_id:
            return info
    return None
