"""v7.20（D373）：静态功能模块注册表 / 开关 / 门控 / 调度 / 事件。"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

import backend.config as config
import backend.db as db
from backend.agent import context as ctx
from backend.agent import tools as tools_module
from backend.app import create_app
from backend.features import catalog, events, flags, subscribers
from backend.models import Friend
from backend.scheduler import jobs as scheduler_jobs


@pytest.fixture(autouse=True)
def _reset_feature_state():
    flags.invalidate()
    events.clear()
    yield
    flags.invalidate()
    events.clear()


@pytest.fixture()
def client(tmp_path, monkeypatch):
    db_file = tmp_path / "features.db"
    monkeypatch.setattr(config, "DB_PATH", db_file)
    monkeypatch.setattr(db, "DB_PATH", db_file)
    monkeypatch.setattr(db, "_engine", None)
    monkeypatch.setattr(db, "_session_factory", None)
    flags.invalidate()
    events.clear()
    app = create_app()
    with TestClient(app) as c:
        yield c
    flags.invalidate()
    events.clear()


def _friend() -> Friend:
    now = datetime.now(timezone.utc).isoformat()
    return Friend(
        id="f-feature",
        name="汐音",
        personality="安静、会观察",
        custom_instructions="句子短",
        created_at=now,
        updated_at=now,
    )


def test_catalog_covers_every_tool_and_job():
    """每个工具都必须登记归属；登记的工具必须真的存在。"""
    tool_names = {tool.name for tool in tools_module.TOOLS}
    declared_list = [name for feature in catalog.FEATURES for name in feature.tools]
    declared = set(declared_list)
    assert len(declared_list) == len(declared), "工具归属不能重复"
    assert declared == tool_names
    assert len(declared) == len(tool_names)
    for tool in tools_module.TOOLS:
        assert catalog.feature_for_tool(tool.name) is not None

    job_ids = {spec["id"] for spec in scheduler_jobs._job_defs(60)}
    declared_job_list = [name for feature in catalog.FEATURES for name in feature.jobs]
    declared_jobs = set(declared_job_list)
    assert len(declared_job_list) == len(declared_jobs), "任务归属不能重复"
    assert declared_jobs == job_ids
    for job_id in job_ids:
        assert catalog.feature_for_job(job_id) is not None


def test_prompt_blocks_are_owned_once():
    blocks = [block for feature in catalog.FEATURES for block in feature.prompt_blocks]
    assert len(blocks) == len(set(blocks))
    for block in blocks:
        assert catalog.feature_for_prompt_block(block) is not None


def test_features_api_lists_and_toggles(client):
    listed = client.get("/api/features")
    assert listed.status_code == 200, listed.text
    by_id = {item["id"]: item for item in listed.json()["features"]}
    assert by_id["core"]["core"] is True
    assert by_id["life"]["enabled"] is True

    off = client.patch("/api/features/life", json={"enabled": False})
    assert off.status_code == 200, off.text
    assert off.json()["feature"]["enabled"] is False

    listed_again = client.get("/api/features").json()["features"]
    assert next(item for item in listed_again if item["id"] == "life")["enabled"] is False

    core = client.patch("/api/features/core", json={"enabled": False})
    assert core.status_code == 409
    assert core.json()["error"]["code"] == "FEATURE_CORE"

    missing = client.patch("/api/features/nope", json={"enabled": False})
    assert missing.status_code == 404


def test_disabled_feature_blocks_its_http_routes(client):
    created = client.post(
        "/api/friends",
        json={"name": "小敏", "personality": "温柔", "custom_instructions": "短句"},
    )
    assert created.status_code == 201, created.text
    friend_id = created.json()["id"]

    assert client.patch("/api/features/life", json={"enabled": False}).status_code == 200
    blocked = client.get(f"/api/life/{friend_id}/status")
    assert blocked.status_code == 409, blocked.text
    assert blocked.json()["error"]["code"] == "FEATURE_DISABLED"

    # 核心聊天入口不受影响
    assert client.get("/api/friends").status_code == 200


def test_tool_prompt_filtering_respects_enabled_features():
    all_lines = tools_module.prompt_lines("normal")
    life_only = tools_module.prompt_lines("normal", enabled_features={"core"})
    assert len(life_only) < len(all_lines)

    with_game = tools_module.prompt_lines(
        "normal", game_active=True, enabled_features={"core", "game"}
    )
    assert any("game_status" in line for line in with_game)
    without_game = tools_module.prompt_lines(
        "normal", game_active=True, enabled_features={"core"}
    )
    assert not any("game_status" in line for line in without_game)


def test_execute_refuses_disabled_tool(monkeypatch):
    flags.set_cache({"core"})
    result = tools_module.execute(None, "f-feature", {"name": "web_search", "arguments": {}})
    assert result["ok"] is False
    assert "关着" in result["error"]


def test_prompt_block_gating():
    friend = _friend()
    without_life = ctx.build_system_prompt(
        friend, life_note="LIFE_MARKER", enabled_features={"core"}
    )
    assert "LIFE_MARKER" not in without_life
    with_life = ctx.build_system_prompt(
        friend, life_note="LIFE_MARKER", enabled_features={"core", "life"}
    )
    assert "LIFE_MARKER" in with_life


def test_scheduler_adds_and_removes_feature_jobs():
    scheduler = scheduler_jobs.build_scheduler(
        heartbeat_minutes=60, enabled_features={"core"}
    )
    assert "life_tick" not in {job.id for job in scheduler.get_jobs()}
    assert "schedule_scan" not in {job.id for job in scheduler.get_jobs()}

    scheduler_jobs.sync_scheduler(scheduler, {"core", "life", "schedule"})
    assert {"life_tick", "life_share_tick", "schedule_scan"} <= {
        job.id for job in scheduler.get_jobs()
    }

    scheduler_jobs.sync_scheduler(scheduler, {"core"})
    assert "life_tick" not in {job.id for job in scheduler.get_jobs()}


def test_event_bus_and_chat_subscriber(monkeypatch):
    events.clear()
    seen: list[dict] = []

    def record(event: dict) -> None:
        seen.append(event)

    events.subscribe("demo.ping", record)
    asyncio.run(events.emit_async("demo.ping", value=1))
    assert seen == [{"event": "demo.ping", "value": 1}]

    from backend.services import life_service

    called: list[str] = []
    monkeypatch.setattr(life_service, "chat_started", lambda friend_id: called.append(friend_id))
    subscribers.install()
    asyncio.run(events.emit_async("chat.started", friend_id="f-feature"))
    assert called == ["f-feature"]


def test_pet_shell_api_respects_feature_flag():
    from backend.desktop.pet import ShellApi

    class DummyPet:
        scale = 1.0

        def show(self, _friend_id=None):  # pragma: no cover - 不该被调到
            raise AssertionError("功能关闭时不许打开桌宠")

    flags.set_cache({"core"})
    result = ShellApi(DummyPet()).open_pet()
    assert result["ok"] is False
    assert "关着" in result["error"]
