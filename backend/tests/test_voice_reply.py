"""语音回复（v2 / T2.0-5 / D85）。

盯这几件事（都是用户会直接撞到的）：
· **开了开关 → 每条助手回复都有音频**；开关关着 → 一个字都没多（纯文字照旧）
· 音频**按消息存**（不是只靠合成缓存），历史能一直回放；文件被删 → 404 + 人话
· 服务没开 / 没配音色 → **200 + 人话**，不是 500（D83）
· 「念给我听」幂等；进程重启后卡住的 pending 会被标成可重试
"""

from __future__ import annotations

import io
import time
import wave

import pytest
from fastapi.testclient import TestClient

import backend.config as config
import backend.db as db
from backend.api.deps import get_provider_factory
from backend.app import create_app
from backend.llm.base import ProviderCapabilities, ProviderConfig
from backend.models import Friend, Message
from backend.services import tts_service, voice_reply_service


def _fake_wav(seconds: float = 0.4, rate: int = 8000) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(rate)
        wav.writeframes(b"\x00\x00" * int(rate * seconds))
    return buf.getvalue()


class FakeProvider:
    """假聊天模型：吐一句话就结束（不碰网络）。"""

    def __init__(self, text: str | list[str] = "我在呢。"):
        self._deltas = [text] if isinstance(text, str) else list(text)
        self.config = ProviderConfig(
            name="fake",
            base_url="http://fake.local/v1",
            api_key="sk-fake",
            model="fake-model",
            capabilities=ProviderCapabilities(
                native_tool_calls=False, reasoning=False, reasoning_streamable=False
            ),
        )

    async def chat_stream(self, messages, tools=None, **kwargs):
        for delta in self._deltas:
            yield {"type": "content", "delta": delta}
        yield {"type": "done", "usage": {}, "stop_reason": "stop"}

    async def health_check(self) -> bool:
        return True


@pytest.fixture()
def app_client(tmp_path, monkeypatch):
    db_file = tmp_path / "voice_reply.db"
    monkeypatch.setattr(config, "DB_PATH", db_file)
    monkeypatch.setattr(db, "DB_PATH", db_file)
    monkeypatch.setattr(db, "_engine", None)
    monkeypatch.setattr(db, "_session_factory", None)
    monkeypatch.setattr(config, "USER_DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "AUDIO_DIR", tmp_path / "audio")
    monkeypatch.setattr(config, "TTS_CACHE_DIR", tmp_path / "tts_cache")
    # 新建音色会后台预热（真去调本机 GPT-SoVITS）——掐掉，测试不许依赖本机服务
    monkeypatch.setattr(tts_service, "spawn_prewarm", lambda *_a, **_k: None)
    tts_service.reset_cache()
    app = create_app()
    app.dependency_overrides[get_provider_factory] = lambda: (lambda: FakeProvider())
    with TestClient(app) as client:
        yield app, client


def _make_voice(client, tmp_path, name: str = "阿月的声音") -> str:
    folder = tmp_path / f"voice-{name}"
    folder.mkdir(parents=True, exist_ok=True)
    paths = {}
    for key, filename in (
        ("gpt_weights_path", "x-e10.ckpt"),
        ("sovits_weights_path", "x_e10_s100.pth"),
        ("ref_audio_path", "ref.wav"),
    ):
        path = folder / filename
        path.write_bytes(b"fake")
        paths[key] = str(path)
    created = client.post("/api/voices", json={"name": name, "prompt_text": "你好呀", **paths})
    assert created.status_code == 201, created.text
    return created.json()["id"]


def _make_friend(client, *, voice_id: str | None = None, enabled: bool = False) -> str:
    friend_id = client.post("/api/friends", json={"name": "阿月", "personality": "话不多"}).json()["id"]
    if voice_id is not None:
        patched = client.patch(
            f"/api/friends/{friend_id}",
            json={"voice_id": voice_id, "voice_reply_enabled": enabled},
        )
        assert patched.status_code == 200, patched.text
    return friend_id


def _message(message_id: str) -> Message | None:
    with db.session_scope() as session:
        return session.get(Message, message_id)


def _wait_for(predicate, timeout: float = 8.0):
    """等后台合成任务做完（TestClient 的 app 跑在另一个线程的事件循环里）。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        result = predicate()
        if result:
            return result
        time.sleep(0.05)
    return None


def _assistant_message_id(friend_id: str) -> str:
    with db.session_scope() as session:
        row = (
            session.query(Message)
            .filter(Message.friend_id == friend_id, Message.role == "assistant")
            .order_by(Message.created_at.desc())
            .first()
        )
        return row.id


def _add_assistant(friend_id: str, content: str, message_id: str = "manual-1") -> str:
    """直接塞一条助手消息（用来测「念给我听」而不走聊天链路）。"""
    with db.session_scope() as session:
        session.add(
            Message(
                id=message_id,
                friend_id=friend_id,
                role="assistant",
                content=content,
                mode_at_send="normal",
                is_proactive=0,
                stream_status="completed",
                created_at="2026-09-19T00:00:00+00:00",
            )
        )
    return message_id


# ---------- D86：括号里的不念 ----------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("（笑）你好呀", "你好呀"),
        ("(smile) 我在呢", "我在呢"),
        ("（她沉默了一会儿）", ""),
        ("**重要**的事", "重要的事"),
        ("看这个 [链接](https://example.com)", "看这个 链接"),
        ("（小声）我…我喜欢你。", "我…我喜欢你。"),
    ],
)
def test_speakable_text_drops_brackets(raw, expected):
    assert voice_reply_service.speakable_text(raw) == expected


def test_parens_are_not_spoken_and_content_is_untouched(app_client, tmp_path, monkeypatch):
    """TTS 文本剔掉括号，但**消息正文一个字都不能动**（D86）。"""
    _app, client = app_client
    captured: dict = {}

    def _capture(**kwargs):
        captured.update(kwargs)
        return _fake_wav()

    monkeypatch.setattr(tts_service, "synthesize", _capture)
    monkeypatch.setattr(
        tts_service, "status", lambda *_a, **_k: {"available": True, "error": None}
    )
    voice_id = _make_voice(client, tmp_path)
    friend_id = _make_friend(client, voice_id=voice_id, enabled=False)
    message_id = _add_assistant(friend_id, "（歪头看你一眼）你今天过得怎么样？")

    assert client.post(f"/api/friends/{friend_id}/messages/{message_id}/speak").json()["ok"] is True
    assert _wait_for(
        lambda: (_message(message_id) or None) and _message(message_id).audio_status == "ready"
    )

    assert captured["text"] == "你今天过得怎么样？"
    assert _message(message_id).content == "（歪头看你一眼）你今天过得怎么样？"  # 正文没动


def test_description_only_reply_has_nothing_to_say(app_client, tmp_path):
    """整条只有动作/心理描写 → 不合成、不报错，回一句人话（D86 第 6 条）。"""
    _app, client = app_client
    voice_id = _make_voice(client, tmp_path)
    friend_id = _make_friend(client, voice_id=voice_id, enabled=False)
    message_id = _add_assistant(friend_id, "（她沉默了一会儿，把脸别过去）")

    result = client.post(f"/api/friends/{friend_id}/messages/{message_id}/speak")
    assert result.status_code == 200, result.text
    body = result.json()
    assert body["ok"] is False
    assert body["nothing_to_say"] is True
    assert "心里的想法" in body["hint"]
    assert _message(message_id).audio_status is None  # 没留下 pending / failed


async def test_schedule_skips_description_only(app_client, tmp_path):
    """自动配音这条路也不该给"只有动作"的消息排队。"""
    _app, client = app_client
    voice_id = _make_voice(client, tmp_path)
    friend_id = _make_friend(client, voice_id=voice_id, enabled=True)
    message_id = _add_assistant(friend_id, "（她笑了）", message_id="desc-1")

    assert voice_reply_service.schedule(message_id) is False


# ---------- D88：边流边合成（Level 1）----------


def test_extract_ready_segments_cuts_at_sentence_ends():
    raw = "今天天气不错，我们出去走走吧。你想去哪里？随便你挑"
    pieces, cursor = voice_reply_service.extract_ready_segments(raw, 0)
    assert pieces == ["今天天气不错，我们出去走走吧。", "你想去哪里？"]
    assert cursor == len("今天天气不错，我们出去走走吧。你想去哪里？")
    # 游标之后新流进来的接着说
    more, cursor2 = voice_reply_service.extract_ready_segments(raw + "，我都听你的。", cursor)
    assert more == ["随便你挑，我都听你的。"]
    assert cursor2 == len(raw + "，我都听你的。")


def test_extract_never_cuts_inside_brackets():
    """括号里的句号不算句子结束（否则会把半句动作描写念出来，违反 D86）。"""
    raw = "（她歪头看你一眼。然后笑了）你好呀，今天过得怎么样？"
    pieces, cursor = voice_reply_service.extract_ready_segments(raw, 0)
    assert pieces == [raw]
    assert cursor == len(raw)
    assert voice_reply_service.speakable_text(pieces[0]) == "你好呀，今天过得怎么样？"


def test_extract_merges_too_short_pieces():
    """太短的碎片不单独切（否则合成次数变多，反而更慢）。"""
    raw = "嗯。好的我知道了。"
    pieces, cursor = voice_reply_service.extract_ready_segments(raw, 0)
    assert pieces == ["嗯。好的我知道了。"]
    assert cursor == len(raw)


def test_extract_paragraphs_merges_bracket_only_lines():
    """D107：只由括号组成的行不算一段 —— 它挂在后面的台词上（合起来才是一个自然段）。"""
    raw = "（她歪头看你一眼）\n\n你好呀，今天过得怎么样？\n\n"
    pieces, cursor = voice_reply_service.extract_ready_paragraphs(raw, 0)
    assert pieces == ["（她歪头看你一眼）\n\n你好呀，今天过得怎么样？"]
    assert cursor == len(raw)


def test_extract_paragraphs_waits_for_the_line_after_a_bracket():
    """只有动作行时**先不切**（要等台词），但也不丢游标。"""
    pieces, cursor = voice_reply_service.extract_ready_paragraphs("（她歪头看你一眼）\n\n", 0)
    assert pieces == [] and cursor == 0

    more, cursor2 = voice_reply_service.extract_ready_paragraphs(
        "（她歪头看你一眼）\n\n你好呀。\n\n", cursor
    )
    assert more == ["（她歪头看你一眼）\n\n你好呀。"]
    assert cursor2 == len("（她歪头看你一眼）\n\n你好呀。\n\n")


async def test_streaming_reply_synthesizes_by_paragraph(app_client, tmp_path, monkeypatch):
    """D88 + D105：文字还在流就按**段**合成，每段落成一个小文件，最后拼成一条。"""
    app, client = app_client
    calls: list[str] = []

    def _synth(**kwargs):
        calls.append(kwargs["text"])
        return _fake_wav(0.3)

    monkeypatch.setattr(tts_service, "synthesize", _synth)
    voice_id = _make_voice(client, tmp_path)
    friend_id = _make_friend(client, voice_id=voice_id, enabled=True)
    app.dependency_overrides[get_provider_factory] = lambda: (
        lambda: FakeProvider(
            ["今天天气不错，", "我们出去走走吧。", "\n\n", "你想去哪里？", "随便你挑，我都听你的。"]
        )
    )

    posted = client.post(f"/api/friends/{friend_id}/messages", json={"content": "在吗"})
    assert posted.status_code == 200
    message_id = _assistant_message_id(friend_id)
    assert _wait_for(
        lambda: (_message(message_id) or None) and _message(message_id).audio_status == "ready"
    )

    # 两段分别合成（不是整段一次）：空行是段的分界
    assert calls == ["今天天气不错，我们出去走走吧。", "你想去哪里？随便你挑，我都听你的。"]
    message = _message(message_id)
    assert message.audio_path == f"audio/{message_id}.wav"
    assert 580 <= (message.audio_duration_ms or 0) <= 620  # 2 × 300ms

    # D105：分段清单（含每段的原文）+ 分段文件都在
    # （清盘和"标 ready"是两步，所以这里也等一下）
    def _segments():
        listed = client.get(f"/api/friends/{friend_id}/messages").json()["messages"]
        item = next(m for m in listed if m["id"] == message_id)
        return item["audio_segments"] if len(item["audio_segments"] or []) == 2 else None

    segments = _wait_for(_segments)
    assert segments
    assert [s["text"] for s in segments] == [
        "今天天气不错，我们出去走走吧。",
        "你想去哪里？随便你挑，我都听你的。",
    ]
    assert [s["index"] for s in segments] == [0, 1]
    for segment in segments:
        assert (tmp_path / "audio" / message_id / f"{segment['index']}.wav").is_file()

    # 分段接口真的取得到那段音频（D105 的新端点）
    one = client.get(f"/api/friends/{friend_id}/messages/{message_id}/audio/1")
    assert one.status_code == 200 and one.headers["content-type"] == "audio/wav"
    assert client.get(f"/api/friends/{friend_id}/messages/{message_id}/audio/9").status_code == 404


async def test_streaming_reply_with_only_brackets_stays_silent(
    app_client, tmp_path, monkeypatch
):
    """整条只有括号（动作 / 心理）→ 一个片段都不合成，状态保持空。"""
    app, client = app_client
    calls: list[str] = []
    monkeypatch.setattr(
        tts_service,
        "synthesize",
        lambda **kwargs: (calls.append(kwargs["text"]), _fake_wav())[1],
    )
    voice_id = _make_voice(client, tmp_path)
    friend_id = _make_friend(client, voice_id=voice_id, enabled=True)
    app.dependency_overrides[get_provider_factory] = lambda: (
        lambda: FakeProvider(["（她", "沉默了一会儿，", "把脸别过去）"])
    )

    posted = client.post(f"/api/friends/{friend_id}/messages", json={"content": "在吗"})
    assert posted.status_code == 200
    message_id = _assistant_message_id(friend_id)
    time.sleep(0.8)  # 给后台收尾留点时间（它应该什么都不做）

    assert calls == []
    listed = client.get(f"/api/friends/{friend_id}/messages").json()["messages"]
    item = next(m for m in listed if m["id"] == message_id)
    assert item["audio_status"] is None
    assert item["audio_url"] is None


# ---------- 自动配音 ----------


def test_reply_gets_audio_when_enabled(app_client, tmp_path, monkeypatch):
    _app, client = app_client
    monkeypatch.setattr(tts_service, "synthesize", lambda **_k: _fake_wav())
    voice_id = _make_voice(client, tmp_path)
    friend_id = _make_friend(client, voice_id=voice_id, enabled=True)

    response = client.post(f"/api/friends/{friend_id}/messages", json={"content": "在吗"})
    assert response.status_code == 200, response.text

    message_id = _assistant_message_id(friend_id)
    ready = _wait_for(
        lambda: (_message(message_id) or None) and _message(message_id).audio_status == "ready"
    )
    assert ready, "回复跑完后应该自动配上语音"

    message = _message(message_id)
    assert message.audio_path == f"audio/{message_id}.wav"
    assert (tmp_path / "audio" / f"{message_id}.wav").is_file()
    assert message.audio_duration_ms and message.audio_duration_ms > 300

    audio = client.get(f"/api/friends/{friend_id}/messages/{message_id}/audio")
    assert audio.status_code == 200
    assert audio.headers["content-type"] == "audio/wav"
    assert len(audio.content) > 100

    # 历史里也要带出来（前端刷新后还能回放）
    listed = client.get(f"/api/friends/{friend_id}/messages").json()["messages"]
    item = next(m for m in listed if m["id"] == message_id)
    assert item["audio_status"] == "ready"
    assert item["audio_url"].endswith(f"/{message_id}/audio")
    assert item["audio_duration_ms"] == message.audio_duration_ms


def test_switch_off_means_pure_text(app_client, tmp_path, monkeypatch):
    _app, client = app_client
    monkeypatch.setattr(tts_service, "synthesize", lambda **_k: _fake_wav())
    voice_id = _make_voice(client, tmp_path)
    friend_id = _make_friend(client, voice_id=voice_id, enabled=False)

    client.post(f"/api/friends/{friend_id}/messages", json={"content": "在吗"})
    time.sleep(0.5)  # 给后台任务"有机会犯错"的时间

    message = _message(_assistant_message_id(friend_id))
    assert message.audio_status is None
    assert not (tmp_path / "audio").exists() or not list((tmp_path / "audio").glob("*.wav"))


def test_synthesis_failure_keeps_text_and_marks_failed(app_client, tmp_path, monkeypatch):
    """合成失败 → 消息还在、文字没动，状态 failed + 人话（用户能点重新合成）。"""
    _app, client = app_client

    def _boom(**_kwargs):
        raise tts_service.ServiceError("TTS_UNREACHABLE", "语音服务连不上——多半是它没开着", 503)

    monkeypatch.setattr(tts_service, "synthesize", _boom)
    voice_id = _make_voice(client, tmp_path)
    friend_id = _make_friend(client, voice_id=voice_id, enabled=True)

    client.post(f"/api/friends/{friend_id}/messages", json={"content": "在吗"})
    message_id = _assistant_message_id(friend_id)
    failed = _wait_for(
        lambda: (_message(message_id) or None) and _message(message_id).audio_status == "failed"
    )
    assert failed
    message = _message(message_id)
    assert message.content == "我在呢。"  # 文字一个字没动
    assert "没开着" in (message.audio_error or "")


# ---------- 「念给我听」----------


def test_speak_without_voice_returns_hint(app_client):
    _app, client = app_client
    friend_id = _make_friend(client)
    client.post(f"/api/friends/{friend_id}/messages", json={"content": "在吗"})
    message_id = _assistant_message_id(friend_id)

    result = client.post(f"/api/friends/{friend_id}/messages/{message_id}/speak")
    assert result.status_code == 200, result.text
    body = result.json()
    assert body["ok"] is False
    assert "音色" in body["hint"]


def test_speak_when_service_down_returns_hint(app_client, tmp_path, monkeypatch):
    _app, client = app_client
    monkeypatch.setattr(
        tts_service,
        "status",
        lambda *_a, **_k: {"available": False, "engine": "gpt-sovits", "base_url": "", "error": "没开着"},
    )
    voice_id = _make_voice(client, tmp_path)
    friend_id = _make_friend(client, voice_id=voice_id, enabled=False)
    client.post(f"/api/friends/{friend_id}/messages", json={"content": "在吗"})
    message_id = _assistant_message_id(friend_id)

    result = client.post(f"/api/friends/{friend_id}/messages/{message_id}/speak")
    assert result.status_code == 200, result.text
    body = result.json()
    assert body["ok"] is False
    assert "没开着" in body["hint"]


def test_speak_is_idempotent(app_client, tmp_path, monkeypatch):
    """开关是关的，但手动「念给我听」照样能配（D85 里说好的"手动强制"）。"""
    _app, client = app_client
    monkeypatch.setattr(tts_service, "synthesize", lambda **_k: _fake_wav())
    monkeypatch.setattr(
        tts_service, "status", lambda *_a, **_k: {"available": True, "error": None}
    )
    voice_id = _make_voice(client, tmp_path)
    friend_id = _make_friend(client, voice_id=voice_id, enabled=False)
    client.post(f"/api/friends/{friend_id}/messages", json={"content": "在吗"})
    message_id = _assistant_message_id(friend_id)

    first = client.post(f"/api/friends/{friend_id}/messages/{message_id}/speak")
    assert first.status_code == 200, first.text
    assert first.json()["ok"] is True
    ready = _wait_for(
        lambda: (_message(message_id) or None) and _message(message_id).audio_status == "ready"
    )
    assert ready

    # 再点一次：不重新合成，直接把现成的给它
    second = client.post(f"/api/friends/{friend_id}/messages/{message_id}/speak")
    body = second.json()
    assert body["ok"] is True
    assert body["audio_status"] == "ready"
    assert body["audio_url"].endswith(f"/{message_id}/audio")


def test_audio_404_when_file_deleted(app_client, tmp_path, monkeypatch):
    """用户说的"除非语音缓存被删了"——文件没了就回人话，别 500。"""
    _app, client = app_client
    monkeypatch.setattr(tts_service, "synthesize", lambda **_k: _fake_wav())
    monkeypatch.setattr(
        tts_service, "status", lambda *_a, **_k: {"available": True, "error": None}
    )
    voice_id = _make_voice(client, tmp_path)
    friend_id = _make_friend(client, voice_id=voice_id, enabled=True)
    client.post(f"/api/friends/{friend_id}/messages", json={"content": "在吗"})
    message_id = _assistant_message_id(friend_id)
    assert _wait_for(
        lambda: (_message(message_id) or None) and _message(message_id).audio_status == "ready"
    )

    (tmp_path / "audio" / f"{message_id}.wav").unlink()

    result = client.get(f"/api/friends/{friend_id}/messages/{message_id}/audio")
    assert result.status_code == 404
    assert result.json()["error"]["code"] == "AUDIO_NOT_FOUND"

    # 历史里 audio_url 也要变成 null（不然前端会去点一个 404 的地址）
    listed = client.get(f"/api/friends/{friend_id}/messages").json()["messages"]
    item = next(m for m in listed if m["id"] == message_id)
    assert item["audio_url"] is None


def test_stale_pending_reset_on_startup(app_client):
    """进程重启后卡在 pending 的（没人会再做完了）→ 标成 failed，用户能重试。"""
    _app, client = app_client
    friend_id = _make_friend(client)
    with db.session_scope() as session:
        message = Message(
            id="stale-1",
            friend_id=friend_id,
            role="assistant",
            content="上次没做完的",
            mode_at_send="normal",
            is_proactive=0,
            stream_status="completed",
            audio_status=voice_reply_service.PENDING,
            created_at="2026-09-19T00:00:00+00:00",
        )
        session.add(message)

    # 起第二个 app（同一份库）模拟"重启"
    app2 = create_app()
    with TestClient(app2):
        pass
    with db.session_scope() as session:
        fresh = session.get(Message, "stale-1")
    assert fresh.audio_status == voice_reply_service.FAILED
    assert "重新合成" in (fresh.audio_error or "")


def test_friend_patch_toggles_voice_reply(app_client, tmp_path):
    _app, client = app_client
    voice_id = _make_voice(client, tmp_path)
    friend_id = _make_friend(client, voice_id=voice_id, enabled=True)

    detail = client.get(f"/api/friends/{friend_id}").json()
    assert detail["voice_reply_enabled"] is True

    client.patch(f"/api/friends/{friend_id}", json={"voice_reply_enabled": False})
    assert client.get(f"/api/friends/{friend_id}").json()["voice_reply_enabled"] is False


def test_speak_rejects_user_message(app_client):
    _app, client = app_client
    friend_id = _make_friend(client)
    client.post(f"/api/friends/{friend_id}/messages", json={"content": "在吗"})
    with db.session_scope() as session:
        user_message = (
            session.query(Message)
            .filter(Message.friend_id == friend_id, Message.role == "user")
            .first()
        )
    result = client.post(f"/api/friends/{friend_id}/messages/{user_message.id}/speak")
    assert result.status_code == 422


def test_message_not_found(app_client):
    _app, client = app_client
    friend_id = _make_friend(client)
    result = client.post(f"/api/friends/{friend_id}/messages/nope/speak")
    assert result.status_code == 404
    assert result.json()["error"]["code"] == "MESSAGE_NOT_FOUND"
