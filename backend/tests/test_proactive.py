"""T5.2 / T5.3 验收：提醒到点 + 主动消息心跳（D17 / D21 / D35）。"""

import random
from datetime import datetime, timedelta, timezone

import pytest

import backend.config as config
import backend.db as db
from backend.llm.base import ProviderCapabilities, ProviderConfig
from backend.models import Friend, Message, Schedule, Thought
from backend.services import (
    character_state_service,
    episode_service,
    memory_service,
    message_service,
    proactive_service,
    schedule_service,
    screen_service,
    settings_service,
)
from backend.services.actor import Actor

NOW = datetime(2026, 9, 19, 12, 0, tzinfo=timezone.utc)


class FakeProvider:
    def __init__(self, says: str = "在忙吗？", error: Exception | None = None):
        self._says = says
        self._error = error
        self.calls: list[list[dict]] = []
        self.config = ProviderConfig(
            name="fake", base_url="http://x", api_key="k", model="m",
            capabilities=ProviderCapabilities(),
        )

    async def chat_stream(self, messages, tools=None, **kwargs):
        self.calls.append(messages)
        if self._error is not None:
            raise self._error
        yield {"type": "content", "delta": self._says}
        yield {"type": "done", "usage": {}, "stop_reason": "stop"}

    async def health_check(self) -> bool:
        return True


@pytest.fixture()
def session(tmp_path, monkeypatch):
    db_file = tmp_path / "proactive.db"
    monkeypatch.setattr(config, "DB_PATH", db_file)
    monkeypatch.setattr(db, "DB_PATH", db_file)
    monkeypatch.setattr(db, "_engine", None)
    monkeypatch.setattr(db, "_session_factory", None)
    from backend.db import Base, get_engine

    Base.metadata.create_all(get_engine())
    return db.session_scope


def _friend(scope, fid="f1", name="阿月", presence=1) -> str:
    with scope() as s:
        s.add(
            Friend(
                id=fid, name=name, presence=presence,
                created_at=NOW.isoformat(), updated_at=NOW.isoformat(),
            )
        )
    return fid


def _message(scope, fid, minutes_ago: int, content="在吗") -> None:
    with scope() as s:
        s.add(
            Message(
                id=f"m-{fid}-{minutes_ago}-{content}",
                friend_id=fid,
                role="user" if content == "在吗" else "assistant",
                content=content,
                created_at=(NOW - timedelta(minutes=minutes_ago)).isoformat(),
            )
        )


# ---------- D17 全体静默 ----------


def test_active_conversation_blocks_everything(session):
    fid = _friend(session)
    _message(session, fid, minutes_ago=1)  # 1 分钟前刚聊过
    with session() as s:
        assert proactive_service.has_active_conversation(s, now=NOW) is True


def test_quiet_after_five_minutes(session):
    fid = _friend(session)
    _message(session, fid, minutes_ago=6)
    with session() as s:
        assert proactive_service.has_active_conversation(s, now=NOW) is False


def test_other_friend_talking_also_silences(session):
    """D17 是**全体**静默：任何人正在聊，所有朋友都不主动。"""
    a = _friend(session, "f1", "阿月")
    _friend(session, "f2", "老王")
    _message(session, a, minutes_ago=1)  # 阿月在聊
    with session() as s:
        result = asyncio_run(
            proactive_service.run_heartbeat_tick(
                s, FakeProvider(), rng=random.Random(1), now=NOW
            )
        )
        assert result == {"sent": 0, "reason": "active_conversation"}


def asyncio_run(coro):
    import asyncio

    return asyncio.get_event_loop_policy().new_event_loop().run_until_complete(coro)


def _prompt_text(call) -> str:
    """把一次调用里的所有消息拍成一段文本（找注入的段落用）。"""
    out: list[str] = []
    for message in call:
        content = message.get("content")
        if isinstance(content, str):
            out.append(content)
        elif isinstance(content, list):
            out.extend(str(item.get("text") or "") for item in content)
    return "\n".join(out)


# ---------- D21 掷骰子 + 在场 ----------


def test_presence_off_never_picked(session):
    _friend(session, "f1", "阿月", presence=0)
    with session() as s:
        assert proactive_service.pick_present_friend(s, rng=random.Random(0)) is None


def test_dice_can_miss(session):
    _friend(session)
    with session() as s:
        # rng 固定 → 掷出 > 0.5 就没人被选中
        rng = random.Random()
        rng.random = lambda: 0.9  # type: ignore[method-assign]
        assert proactive_service.pick_present_friend(s, rng=rng) is None


def test_dice_hit_picks_one(session):
    _friend(session, "f1", "阿月")
    _friend(session, "f2", "老王")
    with session() as s:
        rng = random.Random()
        rng.random = lambda: 0.1  # type: ignore[method-assign]
        picked = proactive_service.pick_present_friend(s, rng=rng)
        assert picked is not None and picked.id in {"f1", "f2"}


# ---------- 主动消息落库 + 未读 ----------


async def test_heartbeat_delivers_and_bumps_unread(session):
    fid = _friend(session)
    _message(session, fid, minutes_ago=30)  # 早就聊完了
    provider = FakeProvider("（戳你一下）在忙吗")
    with session() as s:
        result = await proactive_service.run_heartbeat_tick(
            s, provider, rng=_always_hit(), now=NOW
        )
        assert result["sent"] == 1

    with session() as s:
        friend = s.get(Friend, fid)
        assert friend.unread_count == 1
        rows = [m for m in s.query(Message).filter_by(friend_id=fid).all() if m.is_proactive]
        assert rows and rows[0].content == "（戳你一下）在忙吗"
        assert rows[0].is_proactive == 1
    # 生成时用的是 NORMAL 模式（architecture §6），且没改朋友自己的 mode
    assert "NORMAL（普通）" in provider.calls[0][0]["content"]


def _always_hit() -> random.Random:
    rng = random.Random()
    rng.random = lambda: 0.0  # type: ignore[method-assign]
    return rng


async def test_heartbeat_skips_when_generation_is_empty(session):
    _friend(session)
    _message(session, "f1", minutes_ago=30)
    with session() as s:
        result = await proactive_service.run_heartbeat_tick(
            s, FakeProvider(says="   "), rng=_always_hit(), now=NOW
        )
        assert result == {"sent": 0, "reason": "empty"}


# ---------- D35 日程到点必响，不受在场限制（v4.0 / D243）----------


def _local_day_time(moment: datetime) -> tuple[str, str]:
    local = moment.astimezone()
    return local.strftime("%Y-%m-%d"), local.strftime("%H:%M")


async def test_schedule_reminder_fires_even_when_not_present(session):
    fid = _friend(session, presence=0)  # **不在场**
    day, clock = _local_day_time(NOW - timedelta(minutes=1))
    with session() as s:
        schedule = schedule_service.create(
            s, Actor.USER, fid, title="喝水", date=day, time=clock
        )
        result = await proactive_service.run_schedule_tick(
            s, FakeProvider("喂，该喝水了"), now=NOW
        )
        assert result == {"sent": 1, "noted": 0, "skipped": 0, "due": 1}
        schedule_id = schedule.id

    with session() as s:
        friend = s.get(Friend, fid)
        assert friend.unread_count == 1
        sent = [m for m in s.query(Message).filter_by(friend_id=fid).all() if m.is_proactive]
        assert sent[0].content == "喂，该喝水了"
        # 母表推进到 active，且 first_said_at 记上（缓冲从这一刻起算）
        stored = s.get(Schedule, schedule_id)
        assert stored.state == "active" and stored.first_said_at is not None
        # reminder 说完也进纸条（已说过 → awaiting，别紧接着又问一遍）
        notes = s.query(Thought).filter_by(schedule_id=schedule_id).all()
        assert len(notes) == 1 and notes[0].state == "awaiting"


async def test_date_only_schedule_becomes_a_note_without_sending(session):
    """只到日 = 不响，进纸条由她找机会提（D243）。"""
    fid = _friend(session)
    day, _ = _local_day_time(NOW)
    with session() as s:
        schedule = schedule_service.create(s, Actor.USER, fid, title="买书", date=day)
        result = await proactive_service.run_schedule_tick(s, FakeProvider(), now=NOW)
        assert result == {"sent": 0, "noted": 1, "skipped": 0, "due": 1}
        assert s.get(Schedule, schedule.id).state == "active"
        notes = s.query(Thought).filter_by(schedule_id=schedule.id).all()
        assert len(notes) == 1 and notes[0].state == "pending"
    with session() as s:
        assert [m for m in s.query(Message).filter_by(friend_id=fid).all() if m.is_proactive] == []


async def test_heartbeat_uses_schedule_note_and_marks_schedule_said(session):
    """只到日的日程：到日进纸条 → 心跳里被她说出口 → 母表记 first_said_at（缓冲起算，D243）。"""
    fid = _friend(session)
    _message(session, fid, minutes_ago=30)
    day, _ = _local_day_time(NOW)
    with session() as s:
        schedule = schedule_service.create(s, Actor.USER, fid, title="买书", date=day)
        await proactive_service.run_schedule_tick(s, FakeProvider(), now=NOW)  # 到日 → 进纸条
        assert s.get(Schedule, schedule.id).state == "active"

    with session() as s:
        result = await proactive_service.run_heartbeat_tick(
            s, FakeProvider("对了，书买了吗？"), rng=_always_hit(), now=NOW, screen_watch=False
        )
        assert result["sent"] == 1
        assert s.get(Schedule, schedule.id).first_said_at is not None
        notes = s.query(Thought).filter_by(schedule_id=schedule.id).all()
        assert len(notes) == 1 and notes[0].state == "awaiting"


async def test_future_schedule_does_not_fire(session):
    fid = _friend(session)
    day, clock = _local_day_time(NOW + timedelta(hours=1))
    with session() as s:
        schedule_service.create(s, Actor.USER, fid, title="以后的事", date=day, time=clock)
        assert await proactive_service.run_schedule_tick(s, FakeProvider(), now=NOW) == {
            "sent": 0,
            "noted": 0,
            "skipped": 0,
            "due": 0,
        }


async def test_reminder_is_skipped_when_she_sees_it_is_already_done(session):
    """D245：到点提醒前她看了上下文，判断"已经办完 / 不用提了"（回 `[沉默]`）→ 不发、收尾。"""
    fid = _friend(session)
    day, clock = _local_day_time(NOW - timedelta(minutes=1))
    with session() as s:
        schedule = schedule_service.create(
            s, Actor.USER, fid, title="买书", date=day, time=clock
        )
        result = await proactive_service.run_schedule_tick(
            s, FakeProvider("[沉默]"), now=NOW
        )
        assert result == {"sent": 0, "noted": 0, "skipped": 1, "due": 1}
        assert s.get(Schedule, schedule.id).state == "settled"
    with session() as s:
        # 一条消息都没发（没打扰他）
        assert [m for m in s.query(Message).filter_by(friend_id=fid).all() if m.is_proactive] == []


async def test_schedule_generation_failure_falls_back_to_plain_text(session):
    """生成失败也必须响 —— 提醒不能被吞掉（D35 的精神）。"""
    fid = _friend(session)
    day, clock = _local_day_time(NOW - timedelta(minutes=1))
    with session() as s:
        schedule_service.create(s, Actor.USER, fid, title="交房租", date=day, time=clock)
        result = await proactive_service.run_schedule_tick(
            s, FakeProvider(error=RuntimeError("boom")), now=NOW
        )
        assert result["sent"] == 1
    with session() as s:
        sent = [m for m in s.query(Message).filter_by(friend_id=fid).all() if m.is_proactive]
        assert "交房租" in sent[0].content


async def test_schedule_fires_through_the_silence_rule(session):
    """**到点提醒不受 D17 全体静默影响**（D71）。

    这两条必须同时成立，缺一个都说明规则被改坏了：
      · 有人正在聊 → 主动冒泡被静默挡住（不发）
      · 有人正在聊 → 到点提醒照样响（答应过的事不能因为"你正在说话"就吞掉）
    """
    fid = _friend(session)
    _message(session, fid, minutes_ago=1)  # 1 分钟前刚说过话 = 正在聊
    day, clock = _local_day_time(NOW - timedelta(minutes=1))

    with session() as s:
        schedule_service.create(s, Actor.USER, fid, title="该吃药了", date=day, time=clock)
        # 同一时刻：心跳被静默挡住……
        blocked = await proactive_service.run_heartbeat_tick(
            s, FakeProvider(), rng=random.Random(0), now=NOW
        )
        assert blocked == {"sent": 0, "reason": "active_conversation"}
        # ……但到点提醒必须响
        result = await proactive_service.run_schedule_tick(
            s, FakeProvider("该吃药了。"), now=NOW
        )
        assert result["sent"] == 1

    with session() as s:
        sent = [m for m in s.query(Message).filter_by(friend_id=fid).all() if m.is_proactive]
        assert len(sent) == 1
        assert "该吃药了" in sent[0].content


async def test_heartbeat_job_retries_once_on_failure(monkeypatch):
    """D89：主动消息生成失败要**原地重试一次**，别直接跳过等下个周期（用户没有在等）。

    第一次失败（网络抽风）→ 重试 → 成功发出。
    """
    import asyncio as real_asyncio
    from contextlib import contextmanager

    from backend.scheduler import jobs

    calls: list[int] = []

    async def fake_tick(_session, _provider):  # noqa: ANN001
        calls.append(len(calls) + 1)
        if len(calls) == 1:
            raise RuntimeError("网络抽风")
        return {"sent": 1, "friend": "阿月"}

    @contextmanager
    def fake_scope():
        yield None

    monkeypatch.setattr(jobs, "session_scope", fake_scope)
    monkeypatch.setattr(jobs, "_is_autonomy_owner", lambda: True)
    monkeypatch.setattr(jobs, "_provider_for", lambda _s: object())
    monkeypatch.setattr(jobs.proactive_service, "run_heartbeat_tick", fake_tick)
    real_sleep = real_asyncio.sleep
    monkeypatch.setattr(jobs.asyncio, "sleep", lambda _s: real_sleep(0))

    await jobs._heartbeat_job()
    assert calls == [1, 2], "应该重试一次并成功"


# ---------- v2.2（D103）：看屏 = 主动消息的第一步 ----------


async def test_heartbeat_looks_at_screen_and_uses_it(session, monkeypatch):
    """她主动开口之前先看一眼屏幕，这句观察要进提示词、也要留下来。"""
    screen_service.reset_state()
    fid = _friend(session)
    _message(session, fid, minutes_ago=30)
    shots: list[str] = []
    monkeypatch.setattr(screen_service, "capture", lambda: shots.append("shot") or b"png")

    async def fake_observe(_png, *, session):  # noqa: ANN001
        return "他正在调试代码"

    monkeypatch.setattr(screen_service, "observe", fake_observe)
    provider = FakeProvider("（凑过来看一眼）又在加班？")
    with session() as s:
        result = await proactive_service.run_heartbeat_tick(
            s, provider, rng=_always_hit(), now=NOW, screen_watch=True
        )

    assert result["sent"] == 1 and shots == ["shot"]
    prompt = _prompt_text(provider.calls[0])
    assert "# 你刚看到 TA 的屏幕" in prompt and "他正在调试代码" in prompt
    # 观察留给下次（"最近看到的"，30 分钟有效）
    assert (screen_service.latest(fid) or {}).get("text") == "他正在调试代码"


async def test_heartbeat_silence_means_nothing_is_sent(session, monkeypatch):
    """只观察不说话（D103）：[沉默] → 不落库、不未读、不弹窗，但观察留着。"""
    screen_service.reset_state()
    fid = _friend(session)
    _message(session, fid, minutes_ago=30)
    monkeypatch.setattr(screen_service, "capture", lambda: b"png")

    async def fake_observe(_png, *, session):  # noqa: ANN001
        return "他在看视频"

    monkeypatch.setattr(screen_service, "observe", fake_observe)
    with session() as s:
        result = await proactive_service.run_heartbeat_tick(
            s, FakeProvider("[沉默]"), rng=_always_hit(), now=NOW, screen_watch=True
        )
        assert result == {"sent": 0, "reason": "silent"}

    with session() as s:
        assert s.get(Friend, fid).unread_count == 0
        assert [m for m in s.query(Message).filter_by(friend_id=fid).all() if m.is_proactive] == []
    assert (screen_service.latest(fid) or {}).get("text") == "他在看视频"


async def test_heartbeat_speaks_even_if_screen_lookup_fails(session, monkeypatch):
    """看屏失败（没配视觉 / 截不到屏）**不能**把主动消息一起弄没（D83）。"""
    screen_service.reset_state()
    fid = _friend(session)
    _message(session, fid, minutes_ago=30)

    def boom():
        raise RuntimeError("截不到屏")

    monkeypatch.setattr(screen_service, "capture", boom)
    provider = FakeProvider("在忙吗？")
    with session() as s:
        result = await proactive_service.run_heartbeat_tick(
            s, provider, rng=_always_hit(), now=NOW, screen_watch=True
        )
    assert result["sent"] == 1
    assert "# 你刚看到 TA 的屏幕" not in _prompt_text(provider.calls[0])


async def test_screen_watch_follows_the_setting(session, monkeypatch):
    """开关默认关（不看屏）；打开之后才看。"""
    screen_service.reset_state()
    fid = _friend(session)
    _message(session, fid, minutes_ago=30)
    shots: list[int] = []
    monkeypatch.setattr(screen_service, "capture", lambda: shots.append(1) or b"png")

    async def fake_observe(_png, *, session):  # noqa: ANN001
        return "他在写代码"

    monkeypatch.setattr(screen_service, "observe", fake_observe)

    with session() as s:
        assert settings_service.get(s).screen_watch_enabled == 0  # 默认关
        result = await proactive_service.run_heartbeat_tick(
            s, FakeProvider(), rng=_always_hit(), now=NOW
        )
    assert result["sent"] == 1 and shots == []

    with session() as s:
        settings_service.get(s).screen_watch_enabled = 1
        # 清掉刚才那条主动消息：不然"全体静默"（D17）会挡下这一轮
        s.query(Message).delete()
    with session() as s:
        result = await proactive_service.run_heartbeat_tick(
            s, FakeProvider(), rng=_always_hit(), now=NOW
        )
    assert result["sent"] == 1
    assert shots == [1]


# ---------- D191：主动消息的"分寸"归人格 —— 系统只报事实 ----------


def test_unanswered_state_is_zero_after_the_user_replied(session):
    fid = _friend(session)
    with session() as s:
        s.add(
            Message(
                id="p1", friend_id=fid, role="assistant", content="在忙吗", is_proactive=1,
                created_at=(NOW - timedelta(minutes=40)).isoformat(),
            )
        )
        s.add(
            Message(
                id="u1", friend_id=fid, role="user", content="刚忙完",
                created_at=(NOW - timedelta(minutes=30)).isoformat(),
            )
        )
    with session() as s:
        state = proactive_service.unanswered_state(s, s.get(Friend, fid), now=NOW)
        block = proactive_service.state_block(state)
    assert state["streak"] == 0
    assert "回你了" in block


def test_unanswered_state_counts_consecutive_own_messages(session):
    """TA 没回的这几条要数清楚：条数 / 多久了 / 最后一条说了啥 / 他看到没。"""
    fid = _friend(session)
    with session() as s:
        s.add(
            Message(
                id="u1", friend_id=fid, role="user", content="嗯",
                created_at=(NOW - timedelta(minutes=120)).isoformat(),
            )
        )
        for index, minutes in enumerate((90, 60, 20)):
            s.add(
                Message(
                    id=f"p{index}", friend_id=fid, role="assistant", content=f"第{index}条",
                    is_proactive=1, created_at=(NOW - timedelta(minutes=minutes)).isoformat(),
                )
            )
    with session() as s:
        state = proactive_service.unanswered_state(s, s.get(Friend, fid), now=NOW)
    assert state["streak"] == 3
    assert state["minutes"] == 20
    assert state["last"] == "第2条"
    assert state["unseen"] is False  # unread_count = 0 → 看到了但没回


def test_state_block_reports_facts_without_telling_her_what_to_do(session):
    fid = _friend(session)
    with session() as s:
        s.add(
            Message(
                id="p1", friend_id=fid, role="assistant", content="面试怎么样？", is_proactive=1,
                created_at=(NOW - timedelta(minutes=42)).isoformat(),
            )
        )
        friend = s.get(Friend, fid)
        friend.unread_count = 2
        block = proactive_service.state_block(
            proactive_service.unanswered_state(s, friend, now=NOW)
        )
    assert "42 分钟前" in block and "面试怎么样？" in block
    assert "还没看到" in block
    assert "连续第" not in block  # 只有 1 条，不用报"连续第 N 条"


async def test_heartbeat_hint_carries_the_unanswered_facts(session):
    """心跳给模型的提示词里要带上事实 + 那条"禁复读"的底线（分寸由她自己定）。"""
    fid = _friend(session)
    with session() as s:
        s.add(
            Message(
                id="u1", friend_id=fid, role="user", content="嗯",
                created_at=(NOW - timedelta(minutes=200)).isoformat(),
            )
        )
        for index, minutes in enumerate((90, 60, 30)):
            s.add(
                Message(
                    id=f"p{index}", friend_id=fid, role="assistant", content="在忙吗",
                    is_proactive=1, created_at=(NOW - timedelta(minutes=minutes)).isoformat(),
                )
            )
    provider = FakeProvider("（沉默）")
    with session() as s:
        await proactive_service.run_heartbeat_tick(
            s, provider, rng=_always_hit(), now=NOW, screen_watch=False
        )
    hint = "\n".join(str(m.get("content")) for m in provider.calls[0])
    assert "连续第 3 条" in hint
    assert "别开新话题" in hint
    assert "[沉默]" in hint


async def test_heartbeat_uses_state_episode_and_active_thread(session):
    """V6.0-5（D296/D297/D299）：主动消息要从她自己的状态/情节/工作区里长出来。"""
    fid = _friend(session)
    _message(session, fid, minutes_ago=30)
    with session() as s:
        character_state_service.apply_delta(
            s,
            fid,
            {
                "active_thread": {
                    "goal": "重构日程系统",
                    "constraints": ["不能打断灵感"],
                    "candidates": [{"idea": "只设两个检查点"}],
                    "open_questions": ["ddl 怎么共存"],
                },
                "open_loops_add": ["还没聊完怎么重构日程"],
            },
        )
        episode_service.save(
            s,
            fid,
            kind="episode",
            summary="上次聊到 408 和灵感型学习怎么共存。",
            meaning="他不想被日程表限制。",
        )
    provider = FakeProvider("对了，昨天那个日程的事，我想到一点。")
    with session() as s:
        result = await proactive_service.run_heartbeat_tick(
            s, provider, rng=_always_hit(), now=NOW, screen_watch=False
        )
        assert result["sent"] == 1
    hint = "\n".join(str(m.get("content")) for m in provider.calls[0])
    assert "# 你们正在一起想" in hint
    assert "重构日程系统" in hint
    assert "还没聊完怎么重构日程" in hint
    assert "408 和灵感型学习" in hint


# ---------- D192：小纸条（她攒着想说的那件事）----------


async def test_heartbeat_uses_the_pending_thought_then_marks_it_awaiting(session):
    """心跳：有纸条 → 提示词里带上；她真说了 → 转"待回"（不是销账）。"""
    fid = _friend(session)
    _message(session, fid, minutes_ago=30)  # 早就聊完了（不然 D17 全体静默）
    with session() as s:
        from backend.services import thought_service

        thought, _ = thought_service.add(s, fid, content="问问他面试结果")
    provider = FakeProvider("对了，你面试怎么样？")
    with session() as s:
        result = await proactive_service.run_heartbeat_tick(
            s, provider, rng=_always_hit(), now=NOW, screen_watch=False
        )
        assert result["sent"] == 1
        thought_id = thought.id
    hint = "\n".join(str(m.get("content")) for m in provider.calls[0])
    assert "问问他面试结果" in hint  # 纸条内容进了提示词
    from backend.models import Thought

    with session() as s:
        stored = s.get(Thought, thought_id)
        assert stored.state == "awaiting"  # **说了 ≠ 销账**
        assert stored.said_at is not None


async def test_heartbeat_keeps_the_thought_pending_when_she_stays_silent(session):
    """[沉默] = 这次没说 → 纸条**留在待说**（不白丢）。"""
    fid = _friend(session)
    _message(session, fid, minutes_ago=30)
    with session() as s:
        from backend.services import thought_service

        thought, _ = thought_service.add(s, fid, content="问问他面试结果")
    provider = FakeProvider("（沉默）")
    with session() as s:
        await proactive_service.run_heartbeat_tick(
            s, provider, rng=_always_hit(), now=NOW, screen_watch=False
        )
        stored = s.get(type(thought), thought.id)
        assert stored.state == "pending"
