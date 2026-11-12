"""设置路由（api.md §5）。

安全约定：**永远不把 api_key 明文回给前端**，只回 `api_key_set`。
"""

import asyncio
import json
import time

from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session

from backend.api.deps import get_db
from backend.llm.errors import LLMConfigError
from backend.llm.factory import (
    create_provider,
    create_provider_for_purpose,
    load_provider_configs,
    pick_provider_config,
)
from backend.llm.warmup import spawn_warm_up
from backend.scheduler.jobs import reschedule_heartbeat
from backend.schemas._compat import model_dump
from backend.schemas.settings import PurposeTestIn, SettingsPatch
from backend.services import settings_service
from backend.services.errors import NotFoundError

router = APIRouter(prefix="/api/settings", tags=["settings"])


def _to_dict(row) -> dict:
    return {
        "active_message_frequency": row.active_message_frequency,
        "deep_thinking": bool(row.deep_thinking),
        "show_tool_calls": bool(row.show_tool_calls),
        "llm_active": row.llm_active,
        "llm_bases": settings_service.public_llm_bases_from(row),
        "purposes": settings_service.public_purposes_from(row),
        "purposes_enabled": settings_service.purposes_enabled_from(row),
        "memory_token_budget": row.memory_token_budget,
        # v2（D87）：定时预热
        "warmup_enabled": bool(row.warmup_enabled),
        "warmup_interval_minutes": row.warmup_interval_minutes,
        # v2.1（D99）：文件写操作要不要逐笔点头
        "file_write_approval": bool(row.file_write_approval),
        # v2.1（D100）+ v2.2（D103）：看屏（主动消息的第一步）
        "screen_watch_enabled": bool(row.screen_watch_enabled),
        # v3.0（D141）：消息语音保留多少天（0 = 永久留）
        "voice_keep_days": int(row.voice_keep_days),
        # v3.15（2026-09-26）：开机自启（做成设置项，默认关）
        "auto_start": bool(row.auto_start),
        # v5.0（D255）：游戏里主动说话的间隔（秒；0 = 关）
        "game_chat_interval_seconds": int(row.game_chat_interval_seconds),
        # v5.0（D268）：**已废弃（D272）**；保留字段兼容旧客户端
        "game_idle_self_start_seconds": int(row.game_idle_self_start_seconds),
        "game_idle_self_start_cooldown_seconds": int(
            row.game_idle_self_start_cooldown_seconds
        ),
        # v3.0（B9 / D142）：沉浸模式里"我"叫什么
        "my_name": str(row.my_name or "我"),
        "autonomy_owner_device_id": row.autonomy_owner_device_id,
        "updated_at": row.updated_at,
    }


@router.get("")
def read_settings(session: Session = Depends(get_db)) -> dict:
    """读取设置（api.md §5.1）。"""
    return _to_dict(settings_service.get(session))


@router.patch("")
def update_settings(
    body: SettingsPatch, request: Request, session: Session = Depends(get_db)
) -> dict:
    """修改设置（api.md §5.2）。`llm_bases` 里的 `api_key` 省略 = 沿用旧值。"""
    if body.llm_bases is not None:
        settings_service.merge_llm_bases(
            session,
            [model_dump(base) for base in body.llm_bases],
            active=body.llm_active,
        )
    if body.purposes is not None:
        settings_service.merge_purposes(session, body.purposes)
        if "embedding" in body.purposes:
            # D450：embedding 的 provider / model / 阈值可能变了，清掉懒加载缓存。
            from backend.services import embedding_service  # noqa: PLC0415

            embedding_service.invalidate()
    row = settings_service.patch_settings(
        session,
        active_message_frequency=body.active_message_frequency,
        deep_thinking=body.deep_thinking,
        show_tool_calls=body.show_tool_calls,
        memory_token_budget=body.memory_token_budget,
        auto_start=body.auto_start,
        llm_active=body.llm_active,
        warmup_enabled=body.warmup_enabled,
        warmup_interval_minutes=body.warmup_interval_minutes,
        file_write_approval=body.file_write_approval,
        screen_watch_enabled=body.screen_watch_enabled,
        voice_keep_days=body.voice_keep_days,  # v3.0（D141）
        game_chat_interval_seconds=body.game_chat_interval_seconds,  # v5.0（D255）
        game_autonomy_enabled=body.game_autonomy_enabled,  # v5.0（D256）
        game_auto_free_delay_seconds=body.game_auto_free_delay_seconds,  # v5.0（D257）
        game_autonomy_interval_seconds=body.game_autonomy_interval_seconds,  # v5.0（D258）
        game_idle_self_start_seconds=body.game_idle_self_start_seconds,  # 废弃（D272）
        autonomy_owner_device_id=body.autonomy_owner_device_id,
        game_idle_self_start_cooldown_seconds=body.game_idle_self_start_cooldown_seconds,
        # 传了就用（清空 = 回到默认"我"）；没传（None）表示这次不改
        my_name=(body.my_name.strip() or "我") if body.my_name is not None else None,
    )
    if body.llm_bases is not None or body.purposes is not None:
        # 换了 provider 就把新连接也捂热（D46）
        spawn_warm_up(delay=0)
    if body.active_message_frequency is not None:
        # 频率是给用户调的（D22）：改完立刻生效，不用重启
        scheduler = getattr(request.app.state, "scheduler", None)
        if scheduler is not None:
            reschedule_heartbeat(scheduler, body.active_message_frequency)
    return _to_dict(row)


@router.post("/test")
async def test_purpose(body: PurposeTestIn, session: Session = Depends(get_db)) -> dict:
    """**按用途**测试连通（api.md §5.3，v2）。

    对用户有用的语义：不管好赖**都回 200**，让它能直接显示结果（别抛 500）。
    TTS 那种"服务地址 + 音色"的探活不属于这里，等 T2.0-4 单独做。
    """
    row = settings_service.get(session)
    purposes = settings_service.effective_purposes_from(row)
    if body.purpose == "tts":
        # 语音合成不是 LLM 那一套（它是"服务地址 + 引擎"）——测的是**服务在不在**
        from backend.services import tts_service

        config = tts_service.tts_settings(purposes)
        started_tts = time.monotonic()
        status = tts_service.status(config["base_url"], force=True)
        latency = int((time.monotonic() - started_tts) * 1000)
        if not status["available"]:
            return {"ok": False, "error": status.get("error") or "语音服务连不上", "latency_ms": latency}
        return {
            "ok": True,
            "latency_ms": latency,
            "purpose": "tts",
            "engine": status.get("engine"),
            "base_url": config["base_url"],
            "sample_steps": config["sample_steps"],
        }
    if body.purpose == "embedding":
        # D450：本地 fastembed 或 OpenAI 兼容 embedding —— 测"能不能加载 + 能不能编码"
        from backend.services import embedding_service  # noqa: PLC0415

        stored = purposes.get("embedding")
        stored = stored if isinstance(stored, dict) else {}
        draft = {**stored}
        if body.config:
            draft.update({k: v for k, v in body.config.items() if v is not None})
        cfg = embedding_service.config_from_dict(draft)
        if not cfg.enabled:
            return {"ok": False, "error": "向量检索在设置里是关的", "latency_ms": 0}
        started = time.monotonic()
        ready = await asyncio.to_thread(embedding_service.warm, cfg)
        latency = int((time.monotonic() - started) * 1000)
        if not ready:
            return {
                "ok": False,
                "error": "嵌入模型不可用（没下载 / 地址密钥不对 / 看日志）",
                "latency_ms": latency,
            }
        vectors = await asyncio.to_thread(
            embedding_service.embed_texts,
            ["测试一下向量检索"],
            config_override=cfg,
        )
        if not vectors:
            return {"ok": False, "error": "模型加载了、但编码失败（看日志）", "latency_ms": latency}
        return {
            "ok": True,
            "latency_ms": latency,
            "purpose": "embedding",
            "model_echo": cfg.model,
            "dim": len(vectors[0]),
        }
    if body.config:
        # 用前端传上来的草稿测（还没保存也能测）
        stored = purposes.get(body.purpose)
        stored = stored if isinstance(stored, dict) else {}
        draft = {k: v for k, v in body.config.items() if v is not None}
        if "api_key" not in body.config:
            draft["api_key"] = stored.get("api_key", "")
        purposes = {**purposes, body.purpose: {**stored, **draft}}
    raw = json.dumps(purposes, ensure_ascii=False)
    if body.purpose not in ("chat", "game_fast", "vision", "stt", "image"):
        return {"ok": False, "error": f"「{body.purpose}」这项的连通测试还没做", "latency_ms": 0}
    try:
        provider = create_provider_for_purpose(raw, body.purpose)
    except LLMConfigError as exc:
        return {"ok": False, "error": str(exc), "latency_ms": 0}
    started = time.monotonic()
    try:
        await provider.probe()
    except Exception as exc:  # noqa: BLE001 —— 测试连通要如实回错误
        return {
            "ok": False,
            "error": str(exc),
            "latency_ms": int((time.monotonic() - started) * 1000),
        }
    finally:
        closer = getattr(provider, "aclose", None)
        if closer is not None:
            await closer()
    return {
        "ok": True,
        "latency_ms": int((time.monotonic() - started) * 1000),
        "model_echo": provider.config.model,
        "purpose": body.purpose,
    }


@router.post("/providers/{name}/test")
async def test_provider(name: str, session: Session = Depends(get_db)) -> dict:
    """发一次最小请求验证 base_url / key / model（api.md §5.3）。"""
    bases_json, active = settings_service.llm_config(session)
    try:
        configs = load_provider_configs(bases_json)
    except LLMConfigError as exc:
        # 一个 provider 都没配时，就是"找不到这个 provider"
        raise NotFoundError("PROVIDER_NOT_FOUND", f"还没配置任何 provider（{exc}）") from exc
    if name not in {cfg.name for cfg in configs}:
        raise NotFoundError("PROVIDER_NOT_FOUND", f"没有这个 provider：{name}")
    config = pick_provider_config(configs, name)
    provider = create_provider(bases_json, name)
    started = time.monotonic()
    try:
        await provider.probe()
    except Exception as exc:  # noqa: BLE001 —— 测试连通要如实回错误，不能抛 500
        return {
            "ok": False,
            "error": str(exc),
            "latency_ms": int((time.monotonic() - started) * 1000),
        }
    finally:
        closer = getattr(provider, "aclose", None)
        if closer is not None:
            await closer()
    return {
        "ok": True,
        "latency_ms": int((time.monotonic() - started) * 1000),
        "model_echo": config.model,
        "active": active == name,
    }


@router.get("/setup_status")
def setup_status(session: Session = Depends(get_db)) -> dict:
    """首次配置检测（api.md §5.4）。路由要放后面，别被 `/{name}` 抢走。"""
    return settings_service.setup_status(session)
