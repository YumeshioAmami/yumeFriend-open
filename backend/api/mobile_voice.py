"""手机端语音接口（D304）：云端 STT + 语音状态。"""

from __future__ import annotations

import httpx
from fastapi import APIRouter, File, UploadFile

from backend.db import session_scope
from backend.services import settings_service

router = APIRouter(prefix="/api", tags=["mobile-voice"])


@router.get("/tts/status")
def tts_status() -> dict:
    with session_scope() as session:
        purposes = settings_service.effective_purposes_from(settings_service.get(session))
    conf = purposes.get("tts") or {}
    engine = str(conf.get("engine") or "system")
    return {
        "engine": engine,
        "available": True,
        "cloud_configured": bool(conf.get("base_url") and conf.get("model")),
    }


@router.get("/speech/status")
def speech_status() -> dict:
    with session_scope() as session:
        purposes = settings_service.effective_purposes_from(settings_service.get(session))
    conf = purposes.get("stt") or {}
    configured = bool(conf.get("base_url") and conf.get("model"))
    return {
        "configured": configured,
        "mode": "cloud" if configured else "system",
    }


@router.post("/speech/transcribe")
async def transcribe(file: UploadFile = File(...)) -> dict:
    with session_scope() as session:
        purposes = settings_service.effective_purposes_from(settings_service.get(session))
    conf = purposes.get("stt") or {}
    base_url = str(conf.get("base_url") or "").rstrip("/")
    model = str(conf.get("model") or "").strip()
    if not base_url or not model:
        return {
            "ok": False,
            "hint": "云端语音识别没配置；先用手机系统语音输入，或在设置→语音里填云端 STT。",
        }
    data = await file.read()
    headers = {}
    key = str(conf.get("api_key") or "").strip()
    if key:
        headers["Authorization"] = f"Bearer {key}"
    files = {"file": (file.filename or "audio.m4a", data, file.content_type or "audio/mp4")}
    payload = {"model": model}
    try:
        with httpx.Client(timeout=180.0, trust_env=False) as client:
            response = client.post(
                f"{base_url}/audio/transcriptions",
                data=payload,
                files=files,
                headers=headers,
            )
        if response.status_code >= 400:
            return {"ok": False, "hint": f"云端识别失败（HTTP {response.status_code}）"}
        body = response.json()
        return {"ok": True, "text": str(body.get("text") or "").strip()}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "hint": f"云端识别失败：{exc}"}
