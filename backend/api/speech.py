"""语音输入 + 两个本地语音服务的启停（v2.1 / T2.1-2；api.md 的【v2.1】1）。

**端点都是同步 `def`**：转写要跑 ffmpeg + 等模型，跑在线程池里不堵事件循环。
所有失败一律 **200 + `ok:false` + 人话**（D83）。
"""

from fastapi import APIRouter, Depends, File, UploadFile
from sqlalchemy.orm import Session

from backend.api.deps import get_db
from backend.services import speech_service

router = APIRouter(prefix="/api/speech", tags=["speech"])


@router.get("/services")
def list_services(session: Session = Depends(get_db)) -> dict:
    """两个服务的状态（tts / stt）。"""
    return speech_service.status_all(session)


@router.post("/services/{name}/start")
def start_service(name: str, session: Session = Depends(get_db)) -> dict:
    return speech_service.start(session, name)


@router.post("/services/{name}/stop")
def stop_service(name: str) -> dict:
    return speech_service.stop(name)


@router.post("/transcribe")
def transcribe(file: UploadFile = File(...), session: Session = Depends(get_db)) -> dict:
    """录一段音频 → 文字。服务没开会**自动懒启动**（第一次要等模型加载几秒）。"""
    raw = file.file.read()  # 同步读（UploadFile.read() 是 async 的）
    return speech_service.transcribe(session, raw, filename=file.filename or "rec.webm")
