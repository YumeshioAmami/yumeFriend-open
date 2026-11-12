"""D450：向量模型下载 / 校验 / 安全解压的最小验收（不联网）。"""

from __future__ import annotations

import hashlib
import time
import zipfile

from backend import config
from backend.services import embedding_download_service


def test_download_local_zip_and_mark_installed(tmp_path, monkeypatch):
    archive = tmp_path / "model.zip"
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as bundle:
        bundle.writestr(
            "models--jinaai--jina-embeddings-v2-base-zh/snapshots/test/onnx/model.onnx",
            b"x" * (2 * 1024 * 1024),
        )
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()

    monkeypatch.setattr(config, "MODEL_CACHE_DIR", tmp_path / "models")
    monkeypatch.setattr(config, "EMBEDDING_MODEL_DOWNLOAD_URL", archive.as_uri())
    monkeypatch.setattr(config, "EMBEDDING_MODEL_DOWNLOAD_SHA256", digest)

    embedding_download_service.start_download()
    deadline = time.time() + 10
    while time.time() < deadline:
        status = embedding_download_service.status()
        if status["state"] in {"done", "error"}:
            break
        time.sleep(0.05)

    status = embedding_download_service.status()
    assert status["state"] == "done", status
    assert status["installed"] is True
