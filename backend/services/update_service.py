"""桌面自动更新（D456）。

只更新 PyInstaller 目录模式打包版；源码版只检查版本、给资源页地址，不替换文件。
真正的替换由随包分发的 `yumeFriend-updater.exe` 在主进程退出后完成。
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
import uuid
import zipfile
from datetime import datetime, timezone
from pathlib import Path

from loguru import logger

from backend import config

_lock = threading.Lock()
_history_lock = threading.Lock()
_thread: threading.Thread | None = None
_last_check: dict = {}
_last_check_at = 0.0
_manifest: dict = {}
_state: dict = {
    "state": "idle",  # idle | downloading | extracting | ready | applying | error
    "progress": 0.0,
    "downloaded_bytes": 0,
    "total_bytes": 0,
    "error": None,
    "updated_at": None,
    "staging_dir": None,
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _history_path() -> Path:
    return config.LOG_DIR / "update_history.jsonl"


def _append_history(
    event: str,
    *,
    version: str = "",
    from_version: str = "",
    message: str = "",
) -> None:
    item = {
        "at": _now(),
        "event": event,
        "version": version,
        "from_version": from_version,
        "message": message,
    }
    try:
        config.LOG_DIR.mkdir(parents=True, exist_ok=True)
        with _history_lock, _history_path().open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(item, ensure_ascii=False) + "\n")
    except OSError as exc:
        logger.warning("写更新日志失败：{}", exc)


def history(limit: int = 30) -> list[dict]:
    path = _history_path()
    if not path.is_file():
        return []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()[-limit:]
    except OSError:
        return []
    result: list[dict] = []
    for line in reversed(lines):
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(item, dict):
            result.append(item)
    return result


def _install_dir() -> Path:
    return Path(sys.executable).resolve().parent if config.is_frozen() else config.ROOT_DIR


def _supported() -> bool:
    return config.is_frozen() or config.UPDATE_ALLOW_SOURCE


def _version_tuple(value: str) -> tuple[int, ...]:
    try:
        return tuple(int(part) for part in str(value).strip().split("."))
    except ValueError:
        return (0,)


def _writable_install_dir() -> bool:
    parent = _install_dir().parent
    try:
        parent.mkdir(parents=True, exist_ok=True)
        probe = parent / f".yf-write-probe-{uuid.uuid4().hex}"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink(missing_ok=True)
        return True
    except OSError:
        return False


def _fetch_manifest(*, force: bool = False) -> dict:
    global _manifest, _last_check_at
    now = time.monotonic()
    if not force and _manifest and now - _last_check_at < 60:
        return _manifest
    request = urllib.request.Request(
        config.UPDATE_MANIFEST_URL,
        headers={"User-Agent": f"yumeFriend/{config.APP_VERSION}"},
    )
    with urllib.request.urlopen(request, timeout=20) as response:
        payload = response.read()
    data = json.loads(payload.decode("utf-8"))
    if not isinstance(data, dict):
        raise ValueError("更新清单不是 JSON 对象")
    _manifest = data
    _last_check_at = now
    return data


def _status_payload(*, force: bool = False) -> dict:
    global _last_check
    current = config.APP_VERSION
    supported = _supported()
    writable = _writable_install_dir()
    try:
        manifest = _fetch_manifest(force=force)
        latest = str(manifest.get("version") or "").strip()
        update_available = bool(latest) and _version_tuple(latest) > _version_tuple(current)
        result = {
            "ok": True,
            "supported": supported,
            "can_auto_update": bool(supported and writable),
            "writable": writable,
            "current_version": current,
            "latest_version": latest or current,
            "update_available": update_available,
            "notes": str(manifest.get("notes") or ""),
            "download_url": str(manifest.get("desktop_zip_url") or ""),
            "sha256": str(manifest.get("desktop_sha256") or "").lower(),
            "size": int(manifest.get("desktop_size") or 0),
            "resource_url": config.UPDATE_PAGE_URL,
            "error": None,
        }
    except Exception as exc:  # noqa: BLE001 —— 检查失败不能影响主程序
        result = {
            "ok": False,
            "supported": supported,
            "can_auto_update": False,
            "writable": writable,
            "current_version": current,
            "latest_version": current,
            "update_available": False,
            "notes": "",
            "download_url": "",
            "sha256": "",
            "size": 0,
            "resource_url": config.UPDATE_PAGE_URL,
            "error": f"检查更新失败：{exc}",
        }
    with _lock:
        _last_check = dict(result)
    return {**result, "download": dict(_state)}


def check(*, force: bool = False) -> dict:
    return _status_payload(force=force)


def _update_state(**kwargs) -> None:
    with _lock:
        _state.update(kwargs)
        _state["updated_at"] = _now()


def _safe_extract(archive: Path, destination: Path) -> None:
    destination = destination.resolve()
    with zipfile.ZipFile(archive) as bundle:
        for member in bundle.infolist():
            name = member.filename.replace("\\", "/")
            parts = Path(name).parts
            if not name or name.startswith("/") or ".." in parts:
                raise ValueError(f"更新包包含不安全路径：{member.filename}")
            target = (destination / name).resolve()
            if target != destination and destination not in target.parents:
                raise ValueError(f"更新包越界：{member.filename}")
        bundle.extractall(destination)


def _download_worker() -> None:
    try:
        info = check(force=True)
        if not info.get("update_available"):
            raise ValueError("当前已经是最新版本")
        if not info.get("can_auto_update"):
            raise ValueError("当前安装目录不可写，或源码版不支持自动更新")
        install_dir = _install_dir()
        version = str(info["latest_version"])
        staging_root = install_dir.parent / ".yumeFriend-update"
        staging_root.mkdir(parents=True, exist_ok=True)
        staging = staging_root / version
        if staging.exists():
            shutil.rmtree(staging)
        staging.mkdir(parents=True)
        archive = staging / "package.zip"
        _update_state(
            state="downloading",
            progress=0.0,
            downloaded_bytes=0,
            total_bytes=int(info.get("size") or 0),
            error=None,
            staging_dir=str(staging),
        )
        request = urllib.request.Request(
            str(info["download_url"]),
            headers={"User-Agent": f"yumeFriend/{config.APP_VERSION}"},
        )
        digest = hashlib.sha256()
        downloaded = 0
        with urllib.request.urlopen(request, timeout=60) as response:
            total = int(response.headers.get("Content-Length") or info.get("size") or 0)
            _update_state(total_bytes=total)
            with archive.open("wb") as handle:
                while True:
                    chunk = response.read(1024 * 1024)
                    if not chunk:
                        break
                    handle.write(chunk)
                    digest.update(chunk)
                    downloaded += len(chunk)
                    _update_state(
                        downloaded_bytes=downloaded,
                        progress=round(downloaded / total * 100, 1) if total else 0.0,
                    )
        expected = str(info.get("sha256") or "").lower()
        actual = digest.hexdigest().lower()
        if expected and actual != expected:
            raise ValueError(f"更新包 SHA-256 不一致（拿到 {actual}）")
        _update_state(state="extracting", progress=100.0)
        app_dir = staging / "app"
        _safe_extract(archive, app_dir)
        if not (app_dir / "yumeFriend.exe").is_file():
            raise ValueError("更新包里没有 yumeFriend.exe")
        if not (app_dir / config.UPDATER_EXE_NAME).is_file():
            raise ValueError(f"更新包里没有 {config.UPDATER_EXE_NAME}")
        _update_state(state="ready", error=None)
        _append_history(
            "downloaded",
            version=version,
            from_version=config.APP_VERSION,
        )
        logger.info("桌面更新包已就绪：{}", version)
    except Exception as exc:  # noqa: BLE001
        logger.warning("桌面更新下载失败：{}", exc)
        _update_state(state="error", error=str(exc))


def start_download() -> dict:
    global _thread
    with _lock:
        running = _thread is not None and _thread.is_alive()
        if not running:
            _state.update(
                {
                    "state": "downloading",
                    "progress": 0.0,
                    "downloaded_bytes": 0,
                    "total_bytes": 0,
                    "error": None,
                    "updated_at": _now(),
                }
            )
            _thread = threading.Thread(
                target=_download_worker, name="desktop-update-download", daemon=True
            )
            _thread.start()
    return check(force=False)


def apply_update() -> dict:
    with _lock:
        if _state.get("state") != "ready" or not _state.get("staging_dir"):
            return {**check(force=False), "ok": False, "error": "更新包还没准备好"}
        staging_dir = Path(str(_state["staging_dir"])) / "app"
    updater = staging_dir / config.UPDATER_EXE_NAME
    if not updater.is_file():
        return {**check(force=False), "ok": False, "error": "更新器不存在"}
    temp_updater = Path(tempfile.gettempdir()) / f"yumeFriend-updater-{uuid.uuid4().hex}.exe"
    shutil.copy2(updater, temp_updater)
    log_path = config.LOG_DIR / "updater.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    install_dir = _install_dir()
    info = check(force=False)
    args = [
        str(temp_updater),
        "--pid",
        str(os.getpid()),
        "--install-dir",
        str(install_dir),
        "--staging-dir",
        str(staging_dir),
        "--relaunch",
        str(install_dir / "yumeFriend.exe"),
        "--log",
        str(log_path),
        "--history",
        str(_history_path()),
        "--version",
        str(info.get("latest_version") or ""),
        "--from-version",
        str(info.get("current_version") or config.APP_VERSION),
    ]
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    subprocess.Popen(args, close_fds=True, creationflags=flags)
    _update_state(state="applying")
    _append_history(
        "applying",
        version=str(info.get("latest_version") or ""),
        from_version=str(info.get("current_version") or config.APP_VERSION),
    )

    def _exit_soon() -> None:
        time.sleep(0.6)
        try:
            from backend.desktop.app import request_exit_for_update  # noqa: PLC0415

            request_exit_for_update()
        except Exception:  # noqa: BLE001
            logger.warning("请求桌面外壳退出失败，更新器会等进程结束后继续")

    threading.Thread(target=_exit_soon, daemon=True).start()
    return {**check(force=False), "ok": True}
