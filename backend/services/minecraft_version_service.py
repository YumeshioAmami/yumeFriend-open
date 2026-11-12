"""Minecraft 版本数据包管理（D476）。

适配器代码只有一份；每个 Minecraft 正式版需要的方块 / 物品 / 协议数据
按需下载到 `user_data/minecraft/versions/<version>/minecraft-data/`。
启动前一局时先探测服务器版本，再选择精确、同协议或实验性回退的数据包。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import threading
import time
import urllib.parse
import urllib.request
import uuid
import zipfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from loguru import logger

from backend import config
from backend.services.minecraft_probe import ping_server

MANIFEST_CACHE_SECONDS = 10 * 60
PACK_META_NAME = "_yumefriend_pack.json"

_catalog_lock = threading.Lock()
_catalog_cache: dict[str, Any] | None = None
_catalog_loaded_at = 0.0
_catalog_error: str | None = None

_download_guard = threading.Lock()
_download_lock = threading.Lock()
_download_thread: threading.Thread | None = None
_download_state: dict[str, Any] = {
    "state": "idle",
    "version": None,
    "downloaded_bytes": 0,
    "total_bytes": 0,
    "percent": 0.0,
    "error": None,
    "updated_at": None,
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _version_key(value: str) -> tuple[int, ...]:
    return tuple(int(item) for item in re.findall(r"\d+", value or ""))


def _safe_version(value: str) -> str:
    version = str(value or "").strip()
    if not re.fullmatch(r"[0-9A-Za-z][0-9A-Za-z._-]{0,31}", version):
        raise ValueError(f"版本名不合法：{value!r}")
    return version


def _version_dir(version: str) -> Path:
    return config.MINECRAFT_VERSION_DIR / _safe_version(version)


def pack_path(version: str) -> Path:
    return _version_dir(version) / "minecraft-data"


def pack_installed(version: str) -> bool:
    root = pack_path(version)
    return (
        (root / "package.json").is_file()
        and (root / "index.js").is_file()
        and (root / "minecraft-data" / "data" / "dataPaths.json").is_file()
    )


def _read_pack_meta(version: str) -> dict[str, Any]:
    path = _version_dir(version) / PACK_META_NAME
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def installed_versions() -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    root = config.MINECRAFT_VERSION_DIR
    if not root.is_dir():
        return result
    for child in root.iterdir():
        if not child.is_dir() or child.name.startswith("."):
            continue
        version = child.name
        if not pack_installed(version):
            continue
        meta = _read_pack_meta(version)
        result[version] = {
            "version": version,
            "path": str(pack_path(version)),
            "protocol": meta.get("protocol"),
            "size": _path_size(child),
        }
    return result


def _path_size(path: Path) -> int:
    try:
        return sum(item.stat().st_size for item in path.rglob("*") if item.is_file())
    except OSError:
        return 0


def _read_json_source(url: str, timeout: float = 15.0) -> dict[str, Any]:
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme in {"http", "https"}:
        request = urllib.request.Request(
            url,
            headers={"User-Agent": "yumeFriend/minecraft-version-manifest"},
        )
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read()
    else:
        path = (
            Path(urllib.request.url2pathname(parsed.path))
            if parsed.scheme == "file"
            else Path(url)
        )
        raw = path.read_bytes()
    data = json.loads(raw.decode("utf-8-sig"))
    if not isinstance(data, dict):
        raise ValueError("版本清单不是 JSON 对象")
    return data


def _normalize_manifest(data: dict[str, Any]) -> dict[str, Any]:
    versions: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw in data.get("versions") or []:
        if not isinstance(raw, dict):
            continue
        try:
            version = _safe_version(str(raw.get("version") or ""))
        except ValueError:
            continue
        if version in seen:
            continue
        url = str(raw.get("url") or "").strip()
        sha256 = str(raw.get("sha256") or "").strip().lower()
        if not url or not re.fullmatch(r"[0-9a-f]{64}", sha256):
            continue
        seen.add(version)
        protocol = raw.get("protocol")
        versions.append(
            {
                "version": version,
                "protocol": int(protocol) if isinstance(protocol, (int, float)) else None,
                "url": url,
                "sha256": sha256,
                "size": int(raw.get("size") or 0),
                "tier": str(raw.get("tier") or "compatible"),
            }
        )
    versions.sort(key=lambda item: _version_key(item["version"]), reverse=True)
    return {
        "schema": int(data.get("schema") or 1),
        "minecraft_data_version": str(data.get("minecraft_data_version") or ""),
        "mineflayer_version": str(data.get("mineflayer_version") or ""),
        "generated_at": str(data.get("generated_at") or ""),
        "versions": versions,
    }


def _installed_catalog_entries() -> list[dict[str, Any]]:
    return [
        {
            "version": item["version"],
            "protocol": item.get("protocol"),
            "url": "",
            "sha256": "",
            "size": item.get("size") or 0,
            "tier": "installed",
        }
        for item in installed_versions().values()
    ]


def catalog(*, force: bool = False) -> dict[str, Any]:
    global _catalog_cache, _catalog_loaded_at, _catalog_error
    with _catalog_lock:
        fresh = _catalog_cache is not None and (
            time.monotonic() - _catalog_loaded_at < MANIFEST_CACHE_SECONDS
        )
        if fresh and not force:
            return _catalog_cache

        errors: list[str] = []
        raw: dict[str, Any] | None = None
        source = ""
        try:
            raw = _read_json_source(config.MINECRAFT_VERSION_MANIFEST_URL)
            source = "remote"
        except Exception as exc:  # noqa: BLE001 —— 离线时用随包种子清单
            errors.append(f"远程清单：{exc}")

        if raw is None:
            seed = config.RESOURCE_DIR / "assets" / "minecraft" / "versions.json"
            if seed.is_file():
                try:
                    raw = _read_json_source(str(seed))
                    source = "bundled"
                except Exception as exc:  # noqa: BLE001
                    errors.append(f"随包清单：{exc}")

        normalized = (
            _normalize_manifest(raw)
            if raw is not None
            else {
                "schema": 1,
                "minecraft_data_version": "",
                "mineflayer_version": "",
                "generated_at": "",
                "versions": [],
            }
        )
        known = {item["version"] for item in normalized["versions"]}
        for item in _installed_catalog_entries():
            if item["version"] not in known:
                normalized["versions"].append(item)
        normalized["versions"].sort(
            key=lambda item: _version_key(item["version"]), reverse=True
        )
        normalized["source"] = source
        normalized["error"] = " | ".join(errors) if errors else None
        _catalog_cache = normalized
        _catalog_loaded_at = time.monotonic()
        _catalog_error = normalized["error"]
        return normalized


def _entry(version: str, data: dict[str, Any] | None = None) -> dict[str, Any] | None:
    target = _safe_version(version)
    for item in (data or catalog())["versions"]:
        if item["version"] == target:
            return item
    return None


def status(*, force: bool = False) -> dict[str, Any]:
    data = catalog(force=force)
    installed = installed_versions()
    versions = []
    for item in data["versions"]:
        row = dict(item)
        row["installed"] = item["version"] in installed
        row["installed_size"] = installed.get(item["version"], {}).get("size")
        versions.append(row)
    latest = next((item["version"] for item in versions if item.get("url")), None)
    return {
        "manifest_ok": bool(data["versions"]),
        "manifest_source": data.get("source") or "",
        "manifest_error": data.get("error"),
        "minecraft_data_version": data.get("minecraft_data_version") or "",
        "mineflayer_version": data.get("mineflayer_version") or "",
        "generated_at": data.get("generated_at") or "",
        "latest_version": latest,
        "installed_count": len(installed),
        "versions": versions,
        "download": _download_snapshot(),
    }


def _download_snapshot() -> dict[str, Any]:
    with _download_lock:
        item = dict(_download_state)
    total = int(item.get("total_bytes") or 0)
    downloaded = int(item.get("downloaded_bytes") or 0)
    item["percent"] = round(downloaded / total * 100, 1) if total > 0 else 0.0
    version = str(item.get("version") or "")
    item["installed"] = bool(version and pack_installed(version))
    item["updated_at"] = item.get("updated_at") or _now()
    return item


def download_status(version: str | None = None) -> dict[str, Any]:
    item = _download_snapshot()
    if version:
        target = _safe_version(version)
        item["requested_version"] = target
        item["installed"] = pack_installed(target)
    return item


def _update_download(**kwargs: Any) -> None:
    with _download_lock:
        _download_state.update(kwargs)
        _download_state["updated_at"] = _now()


def _safe_extract(archive: Path, destination: Path) -> None:
    destination = destination.resolve()
    with zipfile.ZipFile(archive) as bundle:
        for member in bundle.infolist():
            name = member.filename.replace("\\", "/")
            if not name or name.startswith("/") or ".." in Path(name).parts:
                raise ValueError(f"版本包包含不安全路径：{member.filename}")
            target = (destination / name).resolve()
            if target != destination and destination not in target.parents:
                raise ValueError(f"版本包越界：{member.filename}")
        bundle.extractall(destination)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _download_worker(item: dict[str, Any]) -> None:
    version = item["version"]
    root = config.MINECRAFT_DIR / "downloads"
    root.mkdir(parents=True, exist_ok=True)
    archive = root / f"minecraft-data-{version}.zip"
    temp = archive.with_suffix(".part")
    staging = config.MINECRAFT_VERSION_DIR / f".{version}.tmp-{uuid.uuid4().hex[:8]}"
    digest = hashlib.sha256()
    try:
        _update_download(
            state="downloading",
            version=version,
            downloaded_bytes=0,
            total_bytes=int(item.get("size") or 0),
            error=None,
        )
        request = urllib.request.Request(
            item["url"],
            headers={"User-Agent": "yumeFriend/minecraft-version-downloader"},
        )
        with urllib.request.urlopen(request, timeout=90) as response:
            total = int(response.headers.get("Content-Length") or item.get("size") or 0)
            _update_download(total_bytes=total)
            downloaded = 0
            with temp.open("wb") as handle:
                while True:
                    chunk = response.read(1024 * 1024)
                    if not chunk:
                        break
                    handle.write(chunk)
                    digest.update(chunk)
                    downloaded += len(chunk)
                    _update_download(downloaded_bytes=downloaded)

        _update_download(state="verifying")
        expected = str(item.get("sha256") or "").lower()
        actual = digest.hexdigest().lower()
        if expected and actual != expected:
            raise ValueError(f"版本包 SHA-256 不一致（拿到 {actual}）")
        shutil.move(str(temp), str(archive))

        _update_download(state="extracting")
        if staging.exists():
            shutil.rmtree(staging)
        staging.mkdir(parents=True, exist_ok=True)
        _safe_extract(archive, staging)
        if not (staging / "minecraft-data" / "package.json").is_file():
            raise ValueError("版本包解压后没有 minecraft-data/package.json")
        (staging / PACK_META_NAME).write_text(
            json.dumps(item, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

        target = _version_dir(version)
        if target.exists():
            shutil.rmtree(target)
        staging.rename(target)
        archive_size = int(item.get("size") or archive.stat().st_size)
        _update_download(
            state="done",
            downloaded_bytes=archive_size,
            total_bytes=archive_size,
            error=None,
        )
        logger.info("Minecraft 版本数据包已安装：{}", version)
    except Exception as exc:  # noqa: BLE001 —— 下载失败要显示在 UI
        logger.warning("Minecraft 版本数据包下载失败（{}）：{}", version, exc)
        _update_download(state="error", error=str(exc))
        if staging.exists():
            shutil.rmtree(staging, ignore_errors=True)
    finally:
        for path in (temp, archive):
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass


def download_and_install(version: str) -> Path:
    """同步确保一个版本已安装；游戏启动前需要等它。"""
    target_version = _safe_version(version)
    if pack_installed(target_version):
        return pack_path(target_version)
    item = _entry(target_version)
    if item is None or not item.get("url"):
        raise ValueError(f"版本清单里没有可下载的 {target_version}")
    with _download_guard:
        if pack_installed(target_version):
            return pack_path(target_version)
        _download_worker(item)
    if not pack_installed(target_version):
        error = _download_snapshot().get("error") or "未知原因"
        raise ValueError(f"{target_version} 版本数据下载失败：{error}")
    return pack_path(target_version)


def start_download(version: str) -> dict[str, Any]:
    global _download_thread
    target_version = _safe_version(version)
    if pack_installed(target_version):
        _update_download(state="done", version=target_version, error=None)
        return _download_snapshot()
    item = _entry(target_version)
    if item is None or not item.get("url"):
        raise ValueError(f"版本清单里没有可下载的 {target_version}")
    with _download_lock:
        running = _download_thread is not None and _download_thread.is_alive()
        if not running:
            _download_state.update(
                {
                    "state": "downloading",
                    "version": target_version,
                    "downloaded_bytes": 0,
                    "total_bytes": int(item.get("size") or 0),
                    "percent": 0.0,
                    "error": None,
                    "updated_at": _now(),
                }
            )
            _download_thread = threading.Thread(
                target=_download_guarded_worker,
                args=(item,),
                name="minecraft-version-download",
                daemon=True,
            )
            _download_thread.start()
    return _download_snapshot()


def _download_guarded_worker(item: dict[str, Any]) -> None:
    with _download_guard:
        _download_worker(item)


def delete_version(version: str) -> dict[str, Any]:
    target = _version_dir(version)
    if not target.exists():
        return {"ok": True, "deleted": False}
    shutil.rmtree(target)
    return {"ok": True, "deleted": True}


def _semver_in_text(text: str) -> str:
    match = re.search(r"\d+(?:\.\d+)+", text or "")
    return match.group(0) if match else ""


@dataclass
class VersionResolution:
    pack_version: str
    pack_protocol: int | None
    package_path: str
    server_version: str
    server_protocol: int | None
    mode: str
    warning: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "pack_version": self.pack_version,
            "pack_protocol": self.pack_protocol,
            "server_version": self.server_version,
            "server_protocol": self.server_protocol,
            "mode": self.mode,
            "warning": self.warning,
        }


def _choose_entry(
    versions: list[dict[str, Any]],
    *,
    server_version: str,
    protocol: int | None,
) -> tuple[dict[str, Any], str, str | None]:
    exact = next((item for item in versions if item["version"] == server_version), None)
    if exact is None and protocol is not None:
        exact = next(
            (item for item in versions if item.get("protocol") == protocol),
            None,
        )
    if exact is not None:
        if exact["version"] == server_version:
            return exact, "exact", None
        return (
            exact,
            "same_protocol",
            f"服务器是 {server_version}，使用同协议号的 {exact['version']} 数据包。",
        )

    server_key = _version_key(server_version)
    candidates = [
        item
        for item in versions
        if (item.get("url") or pack_installed(item["version"]))
        and (not server_key or _version_key(item["version"]) <= server_key)
    ]
    if not candidates:
        candidates = [
            item for item in versions if item.get("url") or pack_installed(item["version"])
        ]
    if not candidates:
        raise ValueError("没有可用的 Minecraft 版本数据包")
    chosen = max(candidates, key=lambda item: _version_key(item["version"]))
    warning = (
        f"服务器是 {server_version}，当前最高可用资源包是 {chosen['version']}。"
        "正在使用实验性跨版本兼容；可能需要服务器安装 ViaVersion，"
        "部分方块 / 物品 / 界面识别可能异常。"
    )
    return chosen, "experimental", warning


def resolve_for_server(
    host: str,
    port: int,
    *,
    requested_version: str = "auto",
) -> VersionResolution:
    data = catalog()
    versions = data["versions"]
    if not versions:
        raise ValueError("拿不到 Minecraft 版本数据清单")

    probe = ping_server(host, port)
    server_version = _semver_in_text(probe["server_version"]) or str(
        probe["server_version"] or "未知版本"
    )
    protocol = probe.get("protocol")
    requested = (requested_version or "auto").strip()

    if requested.lower() not in {"auto", "自动", ""}:
        chosen = _entry(requested, data)
        if chosen is None or not chosen.get("url"):
            raise ValueError(f"版本清单里没有 {requested}")
        warning = None
        mode = "exact"
        if protocol is not None and chosen.get("protocol") != protocol:
            mode = "experimental"
            warning = (
                f"你手动选择了 {chosen['version']} 数据包，但服务器是 "
                f"{server_version}（协议 {protocol} / 数据包协议 {chosen.get('protocol')}）。"
                "这是实验性跨版本连接，可能失败或出现物品识别异常。"
            )
    else:
        chosen, mode, warning = _choose_entry(
            versions,
            server_version=server_version,
            protocol=protocol,
        )

    package = download_and_install(chosen["version"])
    return VersionResolution(
        pack_version=chosen["version"],
        pack_protocol=chosen.get("protocol"),
        package_path=str(package),
        server_version=server_version,
        server_protocol=protocol,
        mode=mode,
        warning=warning,
    )
