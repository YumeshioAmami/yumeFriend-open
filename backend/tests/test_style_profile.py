"""D347：说话方式档案 —— 工具写入、合并、版本、注入、回滚。"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

import backend.config as config
import backend.db as db
from backend.agent import context, tools
from backend.models import Friend, Message
from backend.services import style_profile_service

NOW = datetime(2026, 10, 26, 12, 0, tzinfo=timezone.utc)


@pytest.fixture()
def friends(tmp_path, monkeypatch):
    db_file = tmp_path / "style.db"
    monkeypatch.setattr(config, "DB_PATH", db_file)
    monkeypatch.setattr(db, "DB_PATH", db_file)
    monkeypatch.setattr(db, "_engine", None)
    monkeypatch.setattr(db, "_session_factory", None)
    from backend.db import Base, get_engine

    Base.metadata.create_all(get_engine())
    with db.session_scope() as session:
        session.add(
            Friend(id="f1", name="汐音", created_at=NOW.isoformat(), updated_at=NOW.isoformat())
        )
        session.add(
            Friend(id="f2", name="阿月", created_at=NOW.isoformat(), updated_at=NOW.isoformat())
        )
        session.add(
            Message(
                id="m-user",
                friend_id="f1",
                role="user",
                content="你刚才太长了，像写报告",
                created_at="2026-10-26T11:59:00+00:00",
            )
        )
        session.add(
            Message(
                id="m-assistant",
                friend_id="f1",
                role="assistant",
                content="第一段。第二段。第三段。",
                created_at="2026-10-26T11:58:00+00:00",
            )
        )
    return "f1", "f2"


def _call(**arguments) -> dict:
    return {"id": "tool_reflect", "name": "reflect_speech", "arguments": arguments}


def _add(session, friend_id: str, text: str, section: str = "length") -> dict:
    return tools.execute(
        session,
        friend_id,
        _call(
            changes=[{"action": "add", "section": section, "lesson": text}],
            summary="测试反馈",
        ),
    )


def test_tool_writes_persistent_profile_and_raw_event(friends):
    fid, _ = friends
    with db.session_scope() as session:
        result = _add(session, fid, "日常先回一两句，不总结不建议")
        assert result["ok"] is True
    with db.session_scope() as session:
        profile = style_profile_service.get_profile(session, fid)
        lines = profile["sections"]["length"]
        events = style_profile_service.list_events(session, fid)
    assert profile["version"] == 1
    assert len(lines) == 1
    assert lines[0]["text"] == "日常先回一两句，不总结不建议"
    assert events[0].event_type == "reflect"
    assert "太长了" in (events[0].user_text or "")
    assert "第一段" in (events[0].assistant_excerpt or "")


def test_similar_feedback_merges_instead_of_growing(friends):
    fid, _ = friends
    with db.session_scope() as session:
        assert _add(session, fid, "日常先回一两句，不总结不建议")["ok"] is True
    with db.session_scope() as session:
        assert _add(session, fid, "日常先回一两句，不总结、不建议")["ok"] is True
    with db.session_scope() as session:
        lines = style_profile_service.get_profile(session, fid)["sections"]["length"]
    assert len(lines) == 1
    assert lines[0]["hits"] == 2


def test_update_and_remove_use_stable_line_ids(friends):
    fid, _ = friends
    with db.session_scope() as session:
        _add(session, fid, "日常先回一两句")
    with db.session_scope() as session:
        line_id = style_profile_service.get_profile(session, fid)["sections"]["length"][0]["id"]
        updated = tools.execute(
            session,
            fid,
            _call(
                changes=[
                    {
                        "action": "update",
                        "section": "length",
                        "target_id": line_id,
                        "lesson": "日常多数一两句；明确要展开时再长",
                    }
                ]
            ),
        )
        assert updated["ok"] is True
    with db.session_scope() as session:
        line = style_profile_service.get_profile(session, fid)["sections"]["length"][0]
        assert line["text"] == "日常多数一两句；明确要展开时再长"
        removed = tools.execute(
            session,
            fid,
            _call(
                changes=[
                    {"action": "remove", "section": "length", "target_id": line_id}
                ]
            ),
        )
        assert removed["ok"] is True
    with db.session_scope() as session:
        assert style_profile_service.get_profile(session, fid)["sections"]["length"] == []


def test_full_section_requires_explicit_update_or_remove(friends):
    fid, _ = friends
    for text in ("长度规则 A", "长度规则 B", "长度规则 C"):
        with db.session_scope() as session:
            assert _add(session, fid, text)["ok"] is True
    with db.session_scope() as session:
        fourth = _add(session, fid, "长度规则 D")
        assert fourth["ok"] is False
        assert "满了" in fourth["error"]
        profile = style_profile_service.get_profile(session, fid)
        events = style_profile_service.list_events(session, fid)
    assert len(profile["sections"]["length"]) == 3
    assert len(events) == 3


def test_profile_is_isolated_per_friend(friends):
    fid, other = friends
    with db.session_scope() as session:
        _add(session, fid, "只属于汐音的规则")
    with db.session_scope() as session:
        assert style_profile_service.get_profile(session, other)["sections"]["length"] == []


def test_profile_is_injected_as_current_state_not_as_memory(friends):
    fid, _ = friends
    with db.session_scope() as session:
        _add(session, fid, "日常先回一两句")
    with db.session_scope() as session:
        note = style_profile_service.prompt_note(session, fid)
        line_id = style_profile_service.get_profile(session, fid)["sections"]["length"][0]["id"]
        friend = session.get(Friend, fid)
        prompt = context.build_system_prompt(friend, [], now=NOW, style_profile_note=note)
        messages = context.build_messages(
            friend,
            user_input="在干嘛",
            now=NOW,
            style_profile_note=note,
        )
    assert context.STYLE_PROFILE_HEADER in prompt
    assert "日常先回一两句" in prompt
    assert f"[{line_id}]" in prompt
    assert "不是这一轮的临时提醒" in prompt
    assert context.STYLE_PROFILE_HEADER in messages[0]["content"]


def test_reset_and_rollback_keep_history(friends):
    fid, _ = friends
    with db.session_scope() as session:
        _add(session, fid, "要保留的规则")
    with db.session_scope() as session:
        reset = style_profile_service.reset_profile(session, fid, reason="test")
        assert style_profile_service.get_profile(session, fid)["sections"]["length"] == []
    with db.session_scope() as session:
        style_profile_service.rollback_event(session, fid, reset["event_id"])
        restored = style_profile_service.get_profile(session, fid)
        events = style_profile_service.list_events(session, fid)
    assert [item["text"] for item in restored["sections"]["length"]] == ["要保留的规则"]
    assert {event.event_type for event in events} == {"reflect", "reset", "rollback"}


def test_reflect_speech_is_in_manifest_and_native_schema():
    assert "reflect_speech" in tools.tool_names()
    assert "reflect_speech" in tools.NATIVE_SCHEMAS
    assert "reflect_speech" in "\n".join(tools.prompt_lines("normal"))


def test_unknown_target_id_returns_a_readable_error(friends):
    fid, _ = friends
    with db.session_scope() as session:
        result = tools.execute(
            session,
            fid,
            _call(
                changes=[
                    {
                        "action": "update",
                        "section": "length",
                        "target_id": "not-there",
                        "lesson": "新规则",
                    }
                ]
            ),
        )
    assert result["ok"] is False
    assert "找不到 target_id" in result["error"]


def test_explicit_feedback_detector_is_conservative(friends):
    assert style_profile_service.looks_like_explicit_feedback("你刚才太长了，像写报告") is True
    assert style_profile_service.looks_like_explicit_feedback("以后别老用本鲸鱼，语气自然点") is True
    assert style_profile_service.looks_like_explicit_feedback("你说话方式有点太正式了") is True
    assert style_profile_service.looks_like_explicit_feedback(
        "你还能学习我这个说话方式，然后你还能有记忆"
    ) is False
    assert style_profile_service.looks_like_explicit_feedback("这个视频太长了") is False
    assert style_profile_service.looks_like_explicit_feedback("今天好累") is False


def test_feedback_fallback_compresses_to_one_or_two_rules(friends):
    changes = style_profile_service.fallback_changes("你刚才太长了，像写报告，短一点")
    assert changes
    assert changes[0]["section"] == "length"
    assert all(item["action"] == "add" for item in changes)
    assert len(changes) <= 2


def test_feedback_turn_gets_a_near_generation_reminder(friends):
    fid, _ = friends
    with db.session_scope() as session:
        friend = session.get(Friend, fid)
        messages = context.build_messages(
            friend,
            user_input="你刚才太长了，短一点",
            now=NOW,
            style_feedback_reminder=context.STYLE_FEEDBACK_REMINDER,
        )
    assert any(
        item["role"] == "system" and "reflect_speech" in item["content"]
        for item in messages
    )
