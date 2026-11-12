"""v3.2（D148）：语音同步 —— **文字等她那段语音**，而且一个字都不许丢。

用假的流式合成器（`FakeVoice`）驱动 `sse_stream`，不碰真实 TTS / 网络。
三条不变量：
  ① 开了语音 → 文字按**段落**放（不是逐字抢跑）；
  ② 合成失败 / 一直没信号 / 超时 → 文字**照样补齐**；
  ③ 没开语音 → 完全维持原来的逐字流式。
"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

import backend.config as config
import backend.db as db
from backend.api import messages as messages_api
from backend.app import create_app
from backend.llm.base import ProviderCapabilities, ProviderConfig
from backend.models import Message
from backend.services import friend_service, message_service, voice_reply_service
from backend.services.actor import Actor


class FakeProvider:
    def __init__(self, chunks):
        self._chunks = chunks
        self.config = ProviderConfig(
            name="fake",
            base_url="http://fake.local/v1",
            api_key="sk-fake",
            model="fake-model",
            capabilities=ProviderCapabilities(native_tool_calls=False, reasoning=False),
        )

    async def chat_stream(self, messages, tools=None, **kwargs):
        for chunk in self._chunks:
            yield chunk

    async def health_check(self):
        return True


@pytest.fixture()
def app_client(tmp_path, monkeypatch):
    db_file = tmp_path / "test.db"
    monkeypatch.setattr(config, "DB_PATH", db_file)
    monkeypatch.setattr(db, "DB_PATH", db_file)
    monkeypatch.setattr(db, "_engine", None)
    monkeypatch.setattr(db, "_session_factory", None)
    app = create_app()
    with TestClient(app) as client:
        yield app, client


def _setup(client) -> tuple[str, object, str, str]:
    fid = client.post("/api/friends", json={"name": "阿月", "personality": "话不多。"}).json()["id"]
    with db.session_scope() as session:
        friend = friend_service.get(session, Actor.USER, fid)
        user_message = message_service.create(session, fid, role="user", content="说话")
        assistant = message_service.create(
            session, fid, role="assistant", content="", stream_status=message_service.STREAMING
        )
        return fid, friend, user_message.id, assistant.id


class FakeVoice:
    """假流式合成器：喂进来的文字里出现 `。` 就算"这一段好了"。"""

    def __init__(self, *, notify: bool = True):
        self._notify = None
        self.settled = False
        self.fed: list[str] = []
        self._notify = None
        self._notify_enabled = notify

    def attach(self, queue):
        self._notify = queue

    def feed(self, delta: str) -> None:
        self.fed.append(delta)
        if self._notify_enabled and "。" in delta and self._notify is not None:
            self._notify.put_nowait("segment")

    def mark_mood(self, mood) -> None:  # pragma: no cover - 用不到
        pass

    def abort(self) -> None:
        self.settled = True
        if self._notify is not None:
            self._notify.put_nowait(None)


def _events(frames: list[str]) -> list[tuple[str, dict]]:
    out: list[tuple[str, dict]] = []
    name = ""
    for frame in frames:
        for line in frame.splitlines():
            if line.startswith("event: "):
                name = line[len("event: ") :]
            elif line.startswith("data: "):
                out.append((name, json.loads(line[len("data: ") :])))
    return out


async def _run(friend, uid: str, aid: str, provider, frames_out: list[str]) -> None:
    stream = messages_api.sse_stream(
        friend=friend,
        user_input="说话",
        provider=provider,
        history=[],
        memories=[],
        user_message_id=uid,
        assistant_id=aid,
        deep_thinking=False,
        session=None,
    )
    async for frame in stream:
        frames_out.append(frame)


def _content(events: list[tuple[str, dict]]) -> tuple[str, int]:
    parts = [data["delta"] for name, data in events if name == "content"]
    return "".join(parts), len(parts)


async def test_voiced_reply_releases_text_by_paragraph(app_client, monkeypatch):
    """① 文字按段落放：`。` 之前攒着，合成好了才整段出来。"""
    _, client = app_client
    _, friend, uid, aid = _setup(client)
    provider = FakeProvider(
        [{"type": "content", "delta": char} for char in "第一段。第二段还没说完"]
        + [{"type": "done"}]
    )
    voice = FakeVoice()
    monkeypatch.setattr(voice_reply_service, "open_stream", lambda message_id: voice)

    def fake_launch(builder, full_text):
        builder.settled = True
        builder._notify.put_nowait(None)
        return True

    monkeypatch.setattr(voice_reply_service, "launch_finish", fake_launch)

    frames: list[str] = []
    await _run(friend, uid, aid, provider, frames)
    events = _events(frames)

    text, count = _content(events)
    assert text == "第一段。第二段还没说完"  # 一个字都不丢
    assert count == 2, "应该只在『合成完一段』和『收尾』各放一次，不是逐字抢跑"
    assert events[-1][0] == "done"


async def test_no_voice_still_streams_char_by_char(app_client, monkeypatch):
    """③ 没开语音：维持原来的逐字流式（这条防回归）。"""
    _, client = app_client
    _, friend, uid, aid = _setup(client)
    payload = "第一段。第二段还没说完"
    provider = FakeProvider([{"type": "content", "delta": char} for char in payload] + [{"type": "done"}])
    monkeypatch.setattr(voice_reply_service, "open_stream", lambda message_id: None)

    frames: list[str] = []
    await _run(friend, uid, aid, provider, frames)
    text, count = _content(_events(frames))
    assert text == payload
    assert count == len(payload), "没开语音时应当逐字推"


async def test_voiced_reply_never_loses_text_when_synthesis_hangs(app_client, monkeypatch):
    """② 后台永远不出声 → 到点也得把文字补齐（绝不让聊天干等 / 丢字）。"""
    _, client = app_client
    _, friend, uid, aid = _setup(client)
    payload = "第一段。第二段"
    provider = FakeProvider([{"type": "content", "delta": char} for char in payload] + [{"type": "done"}])
    voice = FakeVoice(notify=False)  # 永远不通知
    monkeypatch.setattr(voice_reply_service, "open_stream", lambda message_id: voice)
    monkeypatch.setattr(messages_api, "VOICE_RELEASE_TIMEOUT_SECONDS", 0.05)

    def fake_launch(builder, full_text):
        return True  # 排上了，但一个信号都没有（模拟 TTS 卡死）

    monkeypatch.setattr(voice_reply_service, "launch_finish", fake_launch)

    frames: list[str] = []
    await _run(friend, uid, aid, provider, frames)
    text, _ = _content(_events(frames))
    assert text == payload
    # 而且落库的是完整正文（最后一道保险）
    with db.session_scope() as session:
        stored = session.get(Message, aid)
    assert stored.content == payload


async def test_voiced_reply_flushes_text_on_error(app_client, monkeypatch):
    """② 出错也要把攒着的文字先给用户，再报错。"""
    _, client = app_client
    _, friend, uid, aid = _setup(client)

    class BoomProvider(FakeProvider):
        async def chat_stream(self, messages, tools=None, **kwargs):
            yield {"type": "content", "delta": "说到一半"}
            raise RuntimeError("上游炸了")

    voice = FakeVoice(notify=False)
    monkeypatch.setattr(voice_reply_service, "open_stream", lambda message_id: voice)
    monkeypatch.setattr(messages_api, "VOICE_RELEASE_TIMEOUT_SECONDS", 0.05)

    frames: list[str] = []
    await _run(friend, uid, aid, BoomProvider([]), frames)
    events = _events(frames)
    text, _ = _content(events)
    assert text == "说到一半"
    assert events[-1][0] == "error"
