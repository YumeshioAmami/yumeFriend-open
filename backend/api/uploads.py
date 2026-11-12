"""图片上传 / 取图（v2.0，T2.0-6；api.md 的【v2】1 与【v2】2）。

上传先落 `uploads/pending/`，**发送消息时才转正**到 `images/`。
"""

import time

from fastapi import APIRouter, File, Query, UploadFile
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse
from loguru import logger

from backend.services import image_service, thumb_service
from backend.services.errors import NotFoundError, ServiceError

router = APIRouter(prefix="/api", tags=["images"])


@router.post("/uploads/image")
async def upload_image(file: UploadFile = File(...)) -> dict:
    """收一张图（PNG / JPG / WEBP，≤10 MB，最长边超 2048 会等比缩小）。

    2026-09-27（D215）：**每次上传都记一条**（收了多少字节 / 存成什么 / 花了多久）。
    以前被拒的上传在日志里一点痕迹都没有 —— 用户说"传不上去"，我们只能干瞪眼。

    另外：解码 + 缩图是**同步的 CPU 活**（一张 7 MB 的图要 1~2 秒），原来直接在
    `async def` 里干 → 把整个事件循环**冻住**（她正在说话的那条 SSE 流也跟着卡）。
    扔到线程池里跑，上传期间别的请求照常。
    """
    started = time.monotonic()
    raw = await file.read()
    try:
        info = await run_in_threadpool(image_service.save_upload, raw)
    except ServiceError as exc:
        logger.warning(
            "图片上传被拒：{}（收到 {} 字节，文件名 {!r}）", exc.message, len(raw), file.filename
        )
        raise
    logger.info(
        "图片上传：{} 字节 → {}（{}×{}，存 {} 字节，耗时 {:.1f}s）",
        len(raw),
        info["id"][:8],
        info["width"],
        info["height"],
        info["bytes"],
        time.monotonic() - started,
    )
    return info


@router.get("/images/{image_id}")
def get_image(
    image_id: str,
    w: int = Query(0, ge=0, le=4096, description="要缩到的最长边（0 = 原图）；手机上拉图慢，气泡用 ~900"),
) -> FileResponse:
    """取图（先找 `images/`，再找 `uploads/pending/`）。id 必须是 32 位十六进制。

    `?w=` 是给手机端的：**按需生成压缩版 + 磁盘缓存 + 让客户端缓存住**（v3.1）。
    """
    path = image_service.path_for(image_id)
    if path is None:
        raise NotFoundError("IMAGE_NOT_FOUND", "这张图不在了")
    served = thumb_service.variant(path, w)
    return FileResponse(
        served,
        media_type=thumb_service.media_type_for(served),
        headers=thumb_service.cache_headers(served),
    )
