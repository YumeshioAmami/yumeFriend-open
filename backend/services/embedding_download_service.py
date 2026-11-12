"""向量模型下载（D450）。

普通设置里的「下载增强模型」走这里：从 yumeBlog 资源页拉一个已经整理好的
fastembed 缓存 zip，校验 SHA-256，再安全解压到模型缓存目录。

为什么打包缓存而不是让 fastembed 自己下 HuggingFace：
  · 用户要求从我们的资源页下载；
  · 国内网络下 HF 经常连不上；
  · 打包后的目录结构和 fastembed 的本地缓存完全一致，解压后即可离线加载。
"""

from __future__ import annotations

import hashlib
import shutil
import threading
import urllib.request
import zipfile
from datetime import datetime, timezone
from pathlib import Path

from loguru import logger

from backend import config

_lock = threading.Lock()
_thread: threading.Thread | None = None
_state: dict = {
    "state": "idle",  # idle | downloading | verifying | extracting | done | error
    "downloaded_bytes": 0,
    "total_bytes": 0,
    "percent": 0.0,
    "error": None,
    "updated_at": None,
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _cache_root() -> Path:
    return config.MODEL_CACHE_DIR


def _model_root() -> Path:
    # 自定义模型名和 HF 源仓库名可能不同（量化模型就是），缓存目录跟源仓库走。
    repo = config.EMBEDDING_MODEL_SOURCE_REPO.replace("/", "--")
    return _cache_root() / f"models--{repo}"


def installed() -> bool:
    """缓存里有没有至少一个像样的 ONNX 文件（不加载模型）。"""
    root = _model_root()
    if not root.is_dir():
        return False
    try:
        return any(path.stat().st_size > 1_000_000 for path in root.rglob("*.onnx"))
    except OSError:
        return False


def installed_for(model: str, cache_dir: str | Path | None = None) -> bool:
    """任意本地 fastembed 模型的安装检查（普通页用它判断要不要提示下载）。"""
    if model == config.EMBEDDING_DEFAULT_MODEL:
        return installed()
    root = Path(cache_dir).expanduser() if cache_dir else config.MODEL_CACHE_DIR
    repo_root = root / f"models--{model.replace('/', '--')}"
    candidates = [repo_root]
    for candidate in candidates:
        try:
            if candidate.is_dir() and any(
                path.stat().st_size > 1_000_000 for path in candidate.rglob("*.onnx")
            ):
                return True
        except OSError:
            continue
    # 也允许把 cache_dir 直接指到一个已经放好 ONNX 的模型目录。
    try:
        if any(
            (root / name).is_file() and (root / name).stat().st_size > 1_000_000
            for name in ("model.onnx", "model_optimized.onnx", "model_quantized.onnx")
        ):
            return True
    except OSError:
        pass
    return False


def _snapshot() -> dict:
    with _lock:
        item = dict(_state)
    total = int(item.get("total_bytes") or 0)
    downloaded = int(item.get("downloaded_bytes") or 0)
    item["percent"] = round(downloaded / total * 100, 1) if total > 0 else 0.0
    item.update(
        {
            "model": config.EMBEDDING_DEFAULT_MODEL,
            "dim": config.EMBEDDING_DEFAULT_DIM,
            "size_mb": config.EMBEDDING_MODEL_DOWNLOAD_SIZE_MB,
            "url": config.EMBEDDING_MODEL_DOWNLOAD_URL,
            "sha256": config.EMBEDDING_MODEL_DOWNLOAD_SHA256,
            "installed": installed(),
        }
    )
    return item


def status() -> dict:
    return _snapshot()


def _update(**kwargs) -> None:
    with _lock:
        _state.update(kwargs)
        _state["updated_at"] = _now()


def _safe_extract(archive: Path, destination: Path) -> None:
    destination = destination.resolve()
    with zipfile.ZipFile(archive) as bundle:
        for member in bundle.infolist():
            name = member.filename.replace("\\", "/")
            if not name or name.startswith("/") or ".." in Path(name).parts:
                raise ValueError(f"模型压缩包包含不安全路径：{member.filename}")
            target = (destination / name).resolve()
            if target != destination and destination not in target.parents:
                raise ValueError(f"模型压缩包越界：{member.filename}")
        bundle.extractall(destination)


def _download_worker() -> None:
    cache_root = _cache_root()
    downloads = cache_root / ".downloads"
    downloads.mkdir(parents=True, exist_ok=True)
    archive = downloads / "embedding-model.zip"
    temp = archive.with_suffix(".part")
    digest = hashlib.sha256()
    try:
        _update(
            state="downloading",
            downloaded_bytes=0,
            total_bytes=0,
            error=None,
        )
        request = urllib.request.Request(
            config.EMBEDDING_MODEL_DOWNLOAD_URL,
            headers={"User-Agent": "yumeFriend/embedding-downloader"},
        )
        with urllib.request.urlopen(request, timeout=60) as response:
            total = int(response.headers.get("Content-Length") or 0)
            _update(total_bytes=total)
            downloaded = 0
            with temp.open("wb") as handle:
                while True:
                    chunk = response.read(1024 * 1024)
                    if not chunk:
                        break
                    handle.write(chunk)
                    digest.update(chunk)
                    downloaded += len(chunk)
                    _update(downloaded_bytes=downloaded)

        _update(state="verifying")
        actual = digest.hexdigest().lower()
        expected = (config.EMBEDDING_MODEL_DOWNLOAD_SHA256 or "").lower()
        if expected and expected != "__fill_me__" and actual != expected:
            raise ValueError(f"模型 SHA-256 不一致（拿到 {actual}）")
        shutil.move(str(temp), str(archive))

        _update(state="extracting")
        _safe_extract(archive, cache_root)
        if not installed():
            raise ValueError("解压完成但没找到 ONNX 模型文件")

        from backend.services import embedding_service  # noqa: PLC0415

        embedding_service.invalidate()
        _update(state="done", error=None)
        logger.info("向量模型下载完成：{}", config.EMBEDDING_DEFAULT_MODEL)
    except Exception as exc:  # noqa: BLE001 —— 下载失败要在设置页显示原因
        logger.warning("向量模型下载失败：{}", exc)
        _update(state="error", error=str(exc))
    finally:
        for path in (temp, archive):
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass


def start_download() -> dict:
    """启动后台下载；重复点击只返回当前状态。"""
    global _thread
    if installed():
        _update(state="done", error=None)
        return _snapshot()
    with _lock:
        running = _thread is not None and _thread.is_alive()
        if not running:
            _state.update(
                {
                    "state": "downloading",
                    "downloaded_bytes": 0,
                    "total_bytes": 0,
                    "percent": 0.0,
                    "error": None,
                    "updated_at": _now(),
                }
            )
            _thread = threading.Thread(
                target=_download_worker, name="embedding-model-download", daemon=True
            )
            _thread.start()
    return _snapshot()
