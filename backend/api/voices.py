"""音色库 + 语音服务（v2，api.md 的【v2】4 /【v2】5）。

两条口径（D83）：
  · **没配 / 连不上都不是"错误"**，而是"这项能力现在没有"——回 200 + 一句人话提示
  · 音色库没配好也不影响聊天：语音相关入口给提示就行
"""

from fastapi import APIRouter, Depends, Response
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session

from backend.api.deps import get_db
from backend.schemas.voice import ScanIn, VoiceCreate, VoiceUpdate
from backend.services import settings_service, tts_service, voice_service
from backend.services.actor import Actor
from backend.services.errors import ServiceError

router = APIRouter(prefix="/api/voices", tags=["voices"])
tts_router = APIRouter(prefix="/api/tts", tags=["tts"])


def _tts_settings(session: Session) -> dict:
    purposes = settings_service.effective_purposes_from(settings_service.get(session))
    return tts_service.tts_settings(purposes)


def _with_usage(session: Session, voices) -> list[dict]:
    counts = voice_service.usage_counts(session)
    items = []
    for voice in voices:
        data = voice_service.to_dict(voice)
        data["used_by"] = counts.get(voice.id, 0)
        items.append(data)
    return items


@router.get("")
def list_voices(session: Session = Depends(get_db)) -> dict:
    return {"voices": _with_usage(session, voice_service.list_voices(session))}


@router.post("/scan")
def scan_voices(body: ScanIn | None = None, session: Session = Depends(get_db)) -> dict:
    """扫 GPT-SoVITS 目录，列出**可以导入**的音色（不落库）。

    目录优先用请求里带的（前端把"还没保存的草稿"传上来，省得用户先按保存）。
    """
    config = _tts_settings(session)
    sovits_dir = (body.sovits_dir if body and body.sovits_dir else config["sovits_dir"]) or ""
    if not sovits_dir.strip():
        return {
            "ok": False,
            "sovits_dir": "",
            "hint": "还没告诉我 GPT-SoVITS 装在哪 —— 在上面那个「程序目录」里填一下（比如 D:\\GPT-SoVITS），再点扫描。",
        }
    known = {v.gpt_weights_path for v in voice_service.list_voices(session)}
    try:
        candidates = voice_service.scan(sovits_dir.strip(), known_gpt=known)
    except ServiceError as exc:
        return {"ok": False, "hint": exc.message, "sovits_dir": sovits_dir}
    # 扫成功了就**顺手把目录记下来**：不然用户"填了但没按保存"，下次又扫不到（踩过一次）
    saved_dir = False
    if body and body.sovits_dir and body.sovits_dir.strip() != config["sovits_dir"]:
        settings_service.merge_purposes(session, {"tts": {"sovits_dir": body.sovits_dir.strip()}})
        saved_dir = True
    return {
        "ok": True,
        "sovits_dir": sovits_dir,
        "saved_dir": saved_dir,
        "candidates": candidates,
    }


@router.post("", status_code=201)
async def create_voice(
    body: VoiceCreate,
    session: Session = Depends(get_db),
) -> dict:
    """新建音色。顺带**后台预热一次**——某个音色第一次合成要加载模型（可能超过 60 秒），
    藏在后台做完，用户点「试听」时基本秒回。"""
    voice = voice_service.create(
        session,
        Actor.USER,
        name=body.name,
        gpt_weights_path=body.gpt_weights_path,
        sovits_weights_path=body.sovits_weights_path,
        ref_audio_path=body.ref_audio_path,
        prompt_text=body.prompt_text,
        speed=body.speed,
    )
    config = _tts_settings(session)
    # 同 upload_avatar：**先提交再起后台任务** —— 预热任务用新会话按 id 读 Voice 行，
    # 没提交就读不到（于是"导入音色后预热"静默失效）
    session.commit()
    tts_service.spawn_prewarm(voice.id, config["base_url"], config["sample_steps"])
    return _with_usage(session, [voice])[0]


@router.patch("/{voice_id}")
def update_voice(
    voice_id: str,
    body: VoiceUpdate,
    session: Session = Depends(get_db),
) -> dict:
    voice = voice_service.update(
        session,
        Actor.USER,
        voice_id,
        name=body.name,
        speed=body.speed,
        prompt_text=body.prompt_text,
    )
    return _with_usage(session, [voice])[0]


@router.delete("/{voice_id}", status_code=204, response_class=Response, response_model=None)
def delete_voice(voice_id: str, session: Session = Depends(get_db)) -> None:
    voice_service.delete(session, Actor.USER, voice_id)


@router.post("/{voice_id}/preview")
def preview_voice(voice_id: str, session: Session = Depends(get_db)) -> dict:
    """试听：合成一句固定的话。

    **服务没开不是错误**（D83）：回 `ok:false` + 一句人话，前端直接显示。
    """
    voice = voice_service.get(session, voice_id)
    config = _tts_settings(session)
    try:
        raw = tts_service.synthesize(
            text=tts_service.PREVIEW_TEXT,
            voice=voice,
            base_url=config["base_url"],
            sample_steps=config["sample_steps"],
        )
    except ServiceError as exc:
        return {"ok": False, "hint": exc.message}

    # 合成结果本来就会进缓存；这里直接把缓存文件名算出来给前端放
    key = tts_service.cache_name_of(
        text=tts_service.PREVIEW_TEXT, voice=voice, sample_steps=config["sample_steps"]
    )
    return {"ok": True, "audio_url": f"/api/tts/audio/{key}.wav", "bytes": len(raw)}


@tts_router.get("/status")
def tts_status(session: Session = Depends(get_db)) -> dict:
    """语音服务在不在（结果缓存 10 秒）。"""
    config = _tts_settings(session)
    status = tts_service.status(config["base_url"])
    return {**status, "sample_steps": config["sample_steps"]}


@tts_router.get("/audio/{name}")
def get_audio(name: str):
    """取合成缓存里的音频（只认 40 位十六进制文件名，防路径穿越）。"""
    path = tts_service.cache_file(name)
    if path is None:
        from backend.services.errors import NotFoundError

        raise NotFoundError("AUDIO_NOT_FOUND", "这段音频不在了（缓存被清过？）")
    return FileResponse(path, media_type="audio/wav", headers={"Cache-Control": "max-age=600"})
