"""游戏陪伴 API（P3 / D250）。只给桌面面板用；她自己的入口是工具 `game_*`。"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Body

from backend.db import session_scope
from backend.game.manager import get_manager
from backend.services import blueprint_store, skill_store

router = APIRouter(prefix="/api/game", tags=["game"])


@router.get("/status")
async def status() -> dict[str, Any]:
    return get_manager().status()


@router.post("/start")
async def start(body: dict = Body(...)) -> dict[str, Any]:
    friend_id = str(body.get("friend_id") or "").strip()
    if not friend_id:
        raise ValueError("start 需要 friend_id")
    game_kind = str(body.get("game_kind") or "minecraft")
    config = {
        key: value
        for key, value in body.items()
        if key not in ("friend_id", "game_kind")
    }
    return await get_manager().start(
        friend_id=friend_id, game_kind=game_kind, config=config
    )


@router.post("/stop")
async def stop() -> dict[str, Any]:
    await get_manager().stop()
    return {"ok": True}


@router.post("/command")
async def command(body: dict = Body(...)) -> dict[str, Any]:
    action = str(body.get("action") or "").strip()
    if not action:
        raise ValueError("command 需要 action")
    args = body.get("args")
    return await get_manager().command(action, args if isinstance(args, dict) else {})


@router.get("/events")
def events(limit: int = 50) -> dict[str, Any]:
    return {"events": get_manager().recent_event_dicts(limit)}


@router.get("/snapshot")
async def snapshot() -> dict[str, Any]:
    return await get_manager().snapshot()


@router.get("/cheats")
def cheats() -> dict[str, Any]:
    """作弊 / 难度配置（D290）。没有局时返回“记住”的那份。"""
    return get_manager().get_cheat_config()


@router.post("/cheats")
def set_cheats(body: dict = Body(...)) -> dict[str, Any]:
    """只给用户界面用；LLM 的工具清单里没有这个入口。"""
    if "profile" not in body and "master_enabled" not in body and "abilities" not in body:
        raise ValueError("cheats 需要至少一个字段：profile / master_enabled / abilities")
    return get_manager().set_cheat_config(
        body,
        persist=bool(body.get("persist", False)),
    )


@router.get("/reflexes")
def reflexes() -> dict[str, Any]:
    return get_manager().get_reflex_config()


@router.post("/reflexes")
async def set_reflexes(body: dict = Body(...)) -> dict[str, Any]:
    if not isinstance(body.get("modes"), dict):
        raise ValueError("set_reflexes 需要 modes")
    return await get_manager().set_reflex_config(body, persist=True)


@router.get("/skills")
def list_skills(friend_id: str) -> dict[str, Any]:
    if not friend_id.strip():
        raise ValueError("list_skills 需要 friend_id")
    with session_scope() as db:
        skills = skill_store.list_skills(db, friend_id)
        failures = skill_store.list_failures(db, friend_id, limit=20)
    return {"skills": skills, "failures": failures}


@router.post("/skills/{skill_id}/enabled")
def set_skill_enabled(
    skill_id: str, body: dict = Body(...), friend_id: str = ""
) -> dict[str, Any]:
    if not friend_id.strip():
        raise ValueError("set_skill_enabled 需要 friend_id")
    with session_scope() as db:
        skill = skill_store.set_enabled(
            db, skill_id, friend_id, bool(body.get("enabled", True))
        )
    return {"skill": skill}


@router.delete("/skills/{skill_id}")
def delete_skill(skill_id: str, friend_id: str = "") -> dict[str, Any]:
    if not friend_id.strip():
        raise ValueError("delete_skill 需要 friend_id")
    with session_scope() as db:
        skill_store.delete_skill(db, skill_id, friend_id)
    return {"ok": True}


@router.post("/skills/{skill_id}/run")
async def run_skill(
    skill_id: str,
    body: dict = Body(default={}),
    friend_id: str = "",
) -> dict[str, Any]:
    if not friend_id.strip():
        raise ValueError("run_skill 需要 friend_id")
    if friend_id != get_manager().friend_id:
        raise ValueError("这一局不是这个朋友；先切到她的对局再试跑")
    params = body.get("params")
    return await get_manager().use_skill_by_id(
        skill_id, params if isinstance(params, dict) else {}
    )


@router.get("/skill-failures")
def skill_failures(friend_id: str, limit: int = 30) -> dict[str, Any]:
    if not friend_id.strip():
        raise ValueError("skill_failures 需要 friend_id")
    with session_scope() as db:
        failures = skill_store.list_failures(db, friend_id, limit=limit)
    return {"failures": failures}


@router.get("/blueprints")
def blueprints(friend_id: str) -> dict[str, Any]:
    if not friend_id.strip():
        raise ValueError("blueprints 需要 friend_id")
    with session_scope() as db:
        return {
            "blueprints": blueprint_store.list_blueprints(db, friend_id),
            "creative": blueprint_store.list_creative(db, friend_id),
            "job": get_manager().status().get("blueprint_job"),
        }


@router.post("/blueprints")
def save_blueprint(body: dict = Body(...)) -> dict[str, Any]:
    friend_id = str(body.get("friend_id") or "").strip()
    raw = body.get("blueprint")
    if not friend_id or not isinstance(raw, dict):
        raise ValueError("save_blueprint 需要 friend_id + blueprint")
    with session_scope() as db:
        row = blueprint_store.save_blueprint(
            db, friend_id=friend_id, raw=raw, source="learned"
        )
    return {"blueprint": row}


@router.post("/blueprints/{blueprint_id}/build")
async def build_blueprint(
    blueprint_id: str, body: dict = Body(default={}), friend_id: str = ""
) -> dict[str, Any]:
    if friend_id != get_manager().friend_id:
        raise ValueError("这一局不是这个朋友；先切到她的对局再建造")
    return await get_manager().blueprint_action(
        "build",
        {
            "blueprint_id": blueprint_id,
            "anchor": body.get("anchor"),
        },
    )


@router.post("/creative/start")
async def creative_start(body: dict = Body(...), friend_id: str = "") -> dict[str, Any]:
    if friend_id != get_manager().friend_id:
        raise ValueError("这一局不是这个朋友")
    return await get_manager().blueprint_action("creative_start", body)


@router.post("/creative/{record_id}/finish")
async def creative_finish(
    record_id: str, body: dict = Body(default={}), friend_id: str = ""
) -> dict[str, Any]:
    if friend_id != get_manager().friend_id:
        raise ValueError("这一局不是这个朋友")
    return await get_manager().blueprint_action(
        "creative_finish",
        {"record_id": record_id, "rating": body.get("rating")},
    )


@router.post("/creative/{record_id}/rate")
def creative_rate(
    record_id: str, body: dict = Body(...), friend_id: str = ""
) -> dict[str, Any]:
    if not friend_id.strip():
        raise ValueError("creative_rate 需要 friend_id")
    rating = int(body.get("rating") or 0)
    if not 1 <= rating <= 5:
        raise ValueError("rating 必须是 1~5")
    with session_scope() as db:
        record = blueprint_store.get_creative(db, record_id)
        if record is None or record["friend_id"] != friend_id:
            raise ValueError("没有这个创作记录")
        row = blueprint_store.creative_rate(db, record_id, rating=rating)
    return {"record": row}
