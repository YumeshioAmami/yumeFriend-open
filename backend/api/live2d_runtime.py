"""Live2D 运行时状态、下载与本地加载（D475）。"""

from __future__ import annotations

import json

from fastapi import APIRouter, Body, HTTPException
from fastapi.responses import FileResponse, StreamingResponse

from backend.services import live2d_runtime_service
from backend.services.errors import NotFoundError

router = APIRouter(prefix="/api/live2d/runtime", tags=["live2d-runtime"])


@router.get("/status")
def runtime_status() -> dict:
    return live2d_runtime_service.status()


@router.post("/download")
def download_runtime(payload: dict = Body(...)) -> StreamingResponse:
    if payload.get("accepted_license") is not True:
        raise HTTPException(
            status_code=400,
            detail="请先阅读并同意 Live2D 官方许可协议",
        )
    async def json_lines():
        async for event in live2d_runtime_service.stream_download_runtime():
            yield json.dumps(event, ensure_ascii=False) + "\n"

    return StreamingResponse(
        json_lines(),
        media_type="application/x-ndjson",
        headers={"Cache-Control": "no-store, max-age=0"},
    )


@router.get("/core.js")
def runtime_core() -> FileResponse:
    path, _source = live2d_runtime_service.resolve_runtime_file()
    if path is None:
        raise NotFoundError(
            "LIVE2D_RUNTIME_NOT_FOUND",
            "还没有安装 Live2D 运行时，请先在 Live2D 设置页下载",
        )
    return FileResponse(
        path,
        media_type="text/javascript",
        headers={"Cache-Control": "no-store, max-age=0"},
    )
