"""Live2D 运行时（Cubism Core）下载与状态。

公开仓库不包含 Cubism Core。用户阅读并同意 Live2D 官方许可协议后，应用从
Live2D 官方托管地址下载到本机 `user_data/live2d/runtime/`，之后浏览器端渲染
动态加载这个本地文件。这里不做镜像、不转存第三方来源。
"""

from __future__ import annotations

import hashlib
import os
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx

from backend import config

RUNTIME_FILENAME = "live2dcubismcore.min.js"

OFFICIAL_DOWNLOAD_PAGE = "https://www.live2d.com/en/sdk/download/web/"
OFFICIAL_CORE_URL = (
    "https://cubism.live2d.com/sdk-web/core/live2dcubismcore.min.js"
)
PROPRIETARY_LICENSE_URL = (
    "https://www.live2d.com/eula/"
    "live2d-proprietary-software-license-agreement_en.html"
)
OPEN_LICENSE_URL = (
    "https://www.live2d.com/eula/"
    "live2d-open-software-license-agreement_en.html"
)

MIN_RUNTIME_BYTES = 32 * 1024
MAX_RUNTIME_BYTES = 8 * 1024 * 1024


def runtime_file() -> Path:
    return config.LIVE2D_DIR / "runtime" / RUNTIME_FILENAME


def _bundled_runtime_files() -> tuple[Path, ...]:
    """仅用于源码版/打包版已经带 Core 的兼容路径；公开版不会命中。"""
    candidates = (
        config.RESOURCE_DIR / "frontend" / "dist" / RUNTIME_FILENAME,
        config.ROOT_DIR / "frontend" / "public" / RUNTIME_FILENAME,
    )
    seen: set[str] = set()
    result: list[Path] = []
    for candidate in candidates:
        key = str(candidate.resolve())
        if key in seen:
            continue
        seen.add(key)
        if candidate.is_file():
            result.append(candidate)
    return tuple(result)


def resolve_runtime_file() -> tuple[Path | None, str]:
    installed = runtime_file()
    if installed.is_file():
        return installed, "installed"
    for candidate in _bundled_runtime_files():
        return candidate, "bundled"
    return None, ""


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _validate_runtime(path: Path) -> None:
    size = path.stat().st_size
    if size < MIN_RUNTIME_BYTES:
        raise ValueError("下载内容不是有效的 Live2D 运行时（文件太小）")
    if size > MAX_RUNTIME_BYTES:
        raise ValueError("Live2D 运行时文件异常大，已停止导入")
    header = path.read_bytes()[:4096]
    if b"Live2D Cubism Core" not in header:
        raise ValueError("官方地址返回的内容不是 Live2D Cubism Core")


def status() -> dict[str, Any]:
    path, source = resolve_runtime_file()
    installed_path = runtime_file()
    available = path is not None
    return {
        "available": available,
        "installed": installed_path.is_file(),
        "source": source or None,
        "size": path.stat().st_size if path is not None else None,
        "sha256": _sha256(path) if path is not None else None,
        "official_download_page": OFFICIAL_DOWNLOAD_PAGE,
        "license_urls": {
            "proprietary": PROPRIETARY_LICENSE_URL,
            "open": OPEN_LICENSE_URL,
        },
    }


async def stream_download_runtime() -> AsyncIterator[dict[str, Any]]:
    """从 Live2D 官方地址下载 Core，并以 NDJSON 事件返回进度。

    这里的 200 响应只代表下载流开始；失败通过 `{"type":"error"}` 事件返回，
    以便前端在进度条旁边直接显示人话错误。
    """
    target = runtime_file()
    target.parent.mkdir(parents=True, exist_ok=True)
    temp = target.with_name(f".{target.name}.{os.getpid()}.part")
    temp.unlink(missing_ok=True)

    received = 0
    try:
        timeout = httpx.Timeout(connect=20.0, read=60.0, write=30.0, pool=30.0)
        headers = {"User-Agent": "yumeFriend Live2D runtime downloader"}
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
            async with client.stream("GET", OFFICIAL_CORE_URL, headers=headers) as response:
                response.raise_for_status()
                content_type = (response.headers.get("content-type") or "").lower()
                if "html" in content_type:
                    raise ValueError("官方地址返回了网页而不是运行时文件")
                total = int(response.headers.get("content-length") or 0)
                with temp.open("wb") as handle:
                    async for chunk in response.aiter_bytes(64 * 1024):
                        if not chunk:
                            continue
                        received += len(chunk)
                        if received > MAX_RUNTIME_BYTES:
                            raise ValueError("Live2D 运行时文件异常大，已停止下载")
                        handle.write(chunk)
                        yield {
                            "type": "progress",
                            "received": received,
                            "total": total,
                        }

        _validate_runtime(temp)
        os.replace(temp, target)
        yield {
            "type": "done",
            "ok": True,
            "size": target.stat().st_size,
            "sha256": _sha256(target),
        }
    except httpx.HTTPStatusError as exc:
        temp.unlink(missing_ok=True)
        yield {
            "type": "error",
            "message": f"Live2D 官方下载地址返回 HTTP {exc.response.status_code}",
        }
    except httpx.RequestError as exc:
        temp.unlink(missing_ok=True)
        yield {
            "type": "error",
            "message": f"连接 Live2D 官方下载地址失败：{exc}",
        }
    except Exception as exc:  # noqa: BLE001 —— 下载线程里要把错误变成事件
        temp.unlink(missing_ok=True)
        yield {"type": "error", "message": str(exc)}
