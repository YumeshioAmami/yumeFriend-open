"""v7.13 / D344：轻聊分流、气泡切分、表达学习、手滑。"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

import backend.config as config
import backend.db as db
from backend.agent import bubble_splitter, pacing
from backend.models import Friend, FriendExpression, Message
from backend.services import expression_service, typo_service
from backend.services import turn_scheduler


class AlwaysTypoRng:
    def random(self):
        return 0.0

    def choice(self, seq):
        return seq[0]


def test_pacing_casual_and_deep():
    casual = pacing.choose_pacing("刚刷到什么好玩的没")
    assert casual.register == "casual"
    assert casual.shape == "one_beat"
    assert "轻聊" in pacing.pacing_note(casual)
    assert "不要把对话压成一声" in pacing.pacing_note(casual)
    deep = pacing.choose_pacing("我今天真的很难受，感觉整个人都要崩溃了，不知道为什么")
    assert deep.register == "deep"
    assert deep.shape == "deep"
    assert "深聊" in pacing.pacing_note(deep)


def test_pacing_uses_reply_shape_and_ignores_her_own_long_history():
    shape = pacing.choose_pacing(
        "今天上班遇到一件很离谱的事，你猜怎么着",
        history=[
            {"role": "assistant", "content": "很长" * 100},
            {"role": "assistant", "content": "还是很长" * 100},
        ],
    )
    assert shape.register == "casual"
    assert shape.shape == "two_beat"
    assert shape.target_bubbles == 2
    assert "最多 3 个" in pacing.reply_shape_note(shape)

    short = pacing.choose_pacing("在干嘛")
    assert short.shape == "one_beat"
    assert "1~2 句自然短话" in pacing.reply_shape_note(short)
    assert "不要只做一个单字/单句反应" in pacing.reply_shape_note(short)


def test_bubble_splitter_turns_paragraph_into_short_messages():
    text = (
        "我刚刷到一个很有意思的视频，讲的是以前的人怎么修房子。"
        "里面有一段特别好笑，说他们拿木头当尺子，结果木头自己弯了。"
        "你要不要看，我可以把链接发你。"
    )
    out = bubble_splitter.split_bubbles(text, max_chars=24, max_bubbles=3)
    assert 1 < len(out) <= 3
    assert "".join(out) == text


def test_bubble_splitter_defaults_to_three_and_never_loses_text():
    segments = [
        "第一句很长很长。",
        "第二句也很长很长。",
        "第三句同样很长。",
        "第四句还要继续。",
    ]
    out = bubble_splitter.split_segments(segments, max_chars=8)
    assert bubble_splitter.DEFAULT_MAX_BUBBLES == 3
    assert len(out) == 3
    assert "".join(out) == "".join(segments)


def test_bubble_splitter_does_not_keep_slightly_long_segments():
    segments = ["这一句有四十多个字但是还没有超过旧版的容忍上限所以以前会原样发出去。"]
    out = bubble_splitter.split_segments(segments, max_chars=20, max_bubbles=3)
    assert len(out) > 1
    assert all(len(item) <= 20 for item in out)
    assert "".join(out) == "".join(segments)


def test_bubble_splitter_balances_overflow_instead_of_stuffing_the_last_bubble():
    text = "一。二。三。四。五。六。"
    out = bubble_splitter.split_bubbles(text, max_chars=4, max_bubbles=3)
    assert len(out) == 3
    assert "".join(out) == text
    assert max(len(item) for item in out) - min(len(item) for item in out) <= 1


def test_typo_is_low_risk_and_skips_digits():
    raw = "我觉得这样真的可以"
    out = typo_service.maybe_typo(raw, rate=1.0, rng=AlwaysTypoRng())
    assert len(out) == len(raw)
    assert out != raw
    assert "2026" not in out
    assert typo_service.maybe_typo("2026 年 10 月", rate=1.0, rng=AlwaysTypoRng()) == "2026 年 10 月"


@pytest.fixture()
def expression_friend(tmp_path, monkeypatch):
    db_file = tmp_path / "humanizer.db"
    monkeypatch.setattr(config, "DB_PATH", db_file)
    monkeypatch.setattr(db, "DB_PATH", db_file)
    monkeypatch.setattr(db, "_engine", None)
    monkeypatch.setattr(db, "_session_factory", None)
    from backend.db import Base, get_engine

    Base.metadata.create_all(get_engine())
    now = datetime.now(timezone.utc).isoformat()
    with db.session_scope() as session:
        session.add(
            Friend(
                id="f-human",
                name="汐音",
                humanizer_enabled=1,
                created_at=now,
                updated_at=now,
            )
        )
        for index in range(4):
            session.add(
                Message(
                    id=f"m-{index}",
                    friend_id="f-human",
                    role="user",
                    content="真离谱 真离谱 太抽象了",
                    created_at=f"2026-10-25T10:0{index}:00+00:00",
                )
            )
    return "f-human"


def test_expression_learning_extracts_short_phrases(expression_friend):
    with db.session_scope() as session:
        friend = session.get(Friend, expression_friend)
        count = expression_service.refresh(session, friend)
        note = expression_service.note_for_prompt(session, expression_friend)
    assert count >= 1
    assert "真离谱" in note or "太抽象" in note


def test_turn_scheduler_serializes_same_friend():
    import asyncio

    async def scenario():
        await turn_scheduler.acquire("f-lock")
        entered = asyncio.Event()

        async def second():
            await turn_scheduler.acquire("f-lock")
            entered.set()
            turn_scheduler.release("f-lock")

        task = asyncio.create_task(second())
        await asyncio.sleep(0.02)
        assert not entered.is_set()
        turn_scheduler.release("f-lock")
        await asyncio.wait_for(task, timeout=1)
        assert entered.is_set()

    asyncio.run(scenario())
