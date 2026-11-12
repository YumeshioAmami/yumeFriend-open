"""v5.1-P4 / D288：技能书持久化、检索、隔离、失败笔记。"""

from __future__ import annotations

import pytest

import backend.config as config
import backend.db as db
from backend.app import create_app
from backend.game.plans import PlanStore
from backend.services import friend_service, skill_store
from backend.services.actor import Actor


@pytest.fixture()
def temp_db(tmp_path, monkeypatch):
    db_file = tmp_path / "test.db"
    monkeypatch.setattr(config, "DB_PATH", db_file)
    monkeypatch.setattr(db, "DB_PATH", db_file)
    monkeypatch.setattr(db, "_engine", None)
    monkeypatch.setattr(db, "_session_factory", None)
    create_app()
    yield db_file


def _verified_plan(store: PlanStore):
    plan = store.create(
        condition="附近有橡木",
        expected="背包里多 4 块橡木",
        checks=[
            {"type": "inventory_has", "item": "oak_log", "count": ">=4"}
        ],
        tasks=[
            {
                "action": "mine",
                "args": {"block": "oak_log", "count": 4},
                "checks": [
                    {"type": "task_result", "path": "mined", "value": ">=4"}
                ],
            },
            {"action": "collect"},
        ],
    )
    store.mark_task(plan.id, 1, status="success", result={"mined": 4})
    store.mark_task(plan.id, 2, status="success", result={"collected": True})
    plan.result = {"verified": True, "checks": []}
    return plan


def test_builtins_are_global_and_learned_skills_are_friend_scoped(temp_db):
    store = PlanStore()
    plan = _verified_plan(store)
    with db.session_scope() as session:
        a = friend_service.create(session, Actor.USER, name="小敏")
        b = friend_service.create(session, Actor.USER, name="阿月")
        learned = skill_store.learn_from_plan(
            session, friend_id=a.id, plan=plan, game_kind="minecraft"
        )
        assert learned is not None
        a_skills = skill_store.list_skills(session, a.id)
        b_skills = skill_store.list_skills(session, b.id)
    assert any(item["source"] == "builtin" for item in a_skills)
    assert any(item["source"] == "builtin" for item in b_skills)
    assert any(item["id"] == learned["id"] for item in a_skills)
    assert not any(item["id"] == learned["id"] for item in b_skills)


def test_only_verified_plan_can_be_learned(temp_db):
    store = PlanStore()
    plan = _verified_plan(store)
    plan.result = {"verified": False}
    with db.session_scope() as session:
        friend = friend_service.create(session, Actor.USER, name="小敏")
        assert (
            skill_store.learn_from_plan(
                session, friend_id=friend.id, plan=plan, game_kind="minecraft"
            )
            is None
        )
    no_checks = store.create(
        condition="空手",
        expected="走过一段路",
        tasks=[{"action": "goto", "args": {"x": 0, "y": 64, "z": 0}}],
    )
    no_checks.result = {"verified": True}
    store.mark_task(no_checks.id, 1, status="success", result={})
    with db.session_scope() as session:
        friend = friend_service.create(session, Actor.USER, name="阿月")
        assert (
            skill_store.learn_from_plan(
                session, friend_id=friend.id, plan=no_checks, game_kind="minecraft"
            )
            is None
        )


def test_search_is_top_k_and_skill_admin_actions_work(temp_db):
    store = PlanStore()
    plan = _verified_plan(store)
    with db.session_scope() as session:
        friend = friend_service.create(session, Actor.USER, name="小敏")
        learned = skill_store.learn_from_plan(
            session, friend_id=friend.id, plan=plan, game_kind="minecraft"
        )
        assert learned is not None
        hits = skill_store.search_skills(
            session, friend.id, "砍橡木", game_kind="minecraft", limit=99
        )
        assert 0 < len(hits) <= 5
        assert any(item["id"] == learned["id"] for item in hits)

        disabled = skill_store.set_enabled(
            session, learned["id"], friend.id, False
        )
        assert disabled["enabled"] is False
        enabled_hits = skill_store.search_skills(
            session, friend.id, "砍橡木", game_kind="minecraft", limit=5
        )
        assert not any(item["id"] == learned["id"] for item in enabled_hits)
        skill_store.delete_skill(session, learned["id"], friend.id)
        assert skill_store.get_skill_by_id(session, learned["id"]) is None


def test_failure_notes_are_recorded_and_deduplicated(temp_db):
    with db.session_scope() as session:
        friend = friend_service.create(session, Actor.USER, name="小敏")
        for _ in range(2):
            skill_store.record_failure(
                session,
                friend_id=friend.id,
                plan_id="plan-1",
                reason="postcondition_failed",
                detail="背包 oak_log = 0，要求 >=4",
            )
        failures = skill_store.list_failures(session, friend.id)
    assert len(failures) == 1
    assert failures[0]["reason"] == "postcondition_failed"
