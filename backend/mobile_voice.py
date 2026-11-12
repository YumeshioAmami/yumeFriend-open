"""手机语音：云端 OpenAI 兼容 TTS + Android 系统 TTS 兜底（D304）。"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

import httpx

from backend.db import session_scope
from backend.services import settings_service


def _config() -> dict:
    with session_scope() as session:
        purposes = settings_service.effective_purposes_from(settings_service.get(session))
    conf = purposes.get("tts")
    return conf if isinstance(conf, dict) else {}


def configured() -> bool:
    return True


def synthesize(text: str) -> bytes:
    text = str(text or "").strip()
    if not text:
        raise ValueError("没有可念的文字")
    conf = _config()
    engine = str(conf.get("engine") or "").strip()
    base_url = str(conf.get("base_url") or "").strip().rstrip("/")
    model = str(conf.get("model") or "").strip()
    if engine == "cloud" and base_url and model:
        return _cloud_tts(text, conf, base_url, model)
    return _system_tts(text)


def _cloud_tts(text: str, conf: dict, base_url: str, model: str) -> bytes:
    headers = {"Content-Type": "application/json"}
    key = str(conf.get("api_key") or "").strip()
    if key:
        headers["Authorization"] = f"Bearer {key}"
    payload = {
        "model": model,
        "input": text,
        "voice": str(conf.get("voice") or "alloy"),
        "response_format": "wav",
    }
    with httpx.Client(timeout=120.0, trust_env=False) as client:
        response = client.post(f"{base_url}/audio/speech", json=payload, headers=headers)
        if response.status_code >= 400:
            detail = response.text[:500]
            raise RuntimeError(f"云端语音合成失败（HTTP {response.status_code}）：{detail}")
        raw = response.content
    if len(raw) < 100:
        raise RuntimeError("云端语音返回了空音频")
    return raw


def _system_tts(text: str) -> bytes:
    try:
        from java import jclass  # noqa: PLC0415

        bridge = jclass("com.yumefriend.app.SystemTtsBridge")
        if not bridge.isReady():
            raise RuntimeError("Android 系统 TTS 还没准备好")
        fd, name = tempfile.mkstemp(suffix=".wav")
        os.close(fd)
        try:
            ok = bool(bridge.synthesizeToFile(text, name))
            if not ok:
                raise RuntimeError("Android 系统 TTS 合成失败")
            raw = Path(name).read_bytes()
        finally:
            Path(name).unlink(missing_ok=True)
        if len(raw) < 100:
            raise RuntimeError("Android 系统 TTS 返回了空音频")
        return raw
    except Exception as exc:  # noqa: BLE001
        if isinstance(exc, RuntimeError):
            raise
        raise RuntimeError(f"系统语音不可用：{exc}") from exc
