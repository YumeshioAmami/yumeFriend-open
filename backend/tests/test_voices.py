"""音色库 + 语音服务（v2 / T2.0-4）。

盯四件事：
· **删音色不能连坐删朋友**（friends.voice_id 置空）
· **扫描导入**认得标准结构，且从参考音频文件名里取台词；`ref_xxx.wav` 这种不算
· 语音服务没开时，`/api/tts/status` 和试听都回**人话**，不是 500（D83）
· 缓存：同一句话同音色不重复合成；缓存文件名不能被拿去乱翻文件
"""

from __future__ import annotations

import io

import pytest
from fastapi.testclient import TestClient

import backend.config as config
import backend.db as db
from backend.app import create_app
from backend.models import Friend
from backend.services import tts_service, voice_service


@pytest.fixture()
def client(tmp_path, monkeypatch):
    db_file = tmp_path / "voices.db"
    monkeypatch.setattr(config, "DB_PATH", db_file)
    monkeypatch.setattr(db, "DB_PATH", db_file)
    monkeypatch.setattr(db, "_engine", None)
    monkeypatch.setattr(db, "_session_factory", None)
    monkeypatch.setattr(config, "TTS_CACHE_DIR", tmp_path / "tts_cache")
    # 新建音色会后台预热（真去调本机的 GPT-SoVITS）——测试里必须掐掉，
    # 否则测试会依赖"这台机器此刻有没有开语音服务"，那样迟早会莫名其妙地慢/红。
    monkeypatch.setattr(tts_service, "spawn_prewarm", lambda *_a, **_k: None)
    tts_service.reset_cache()
    app = create_app()
    with TestClient(app) as c:
        yield c


def _fake_voice_files(tmp_path) -> dict:
    """造三个"像那么回事"的文件（内容无所谓，只要存在）。"""
    tmp_path.mkdir(parents=True, exist_ok=True)
    gpt = tmp_path / "x-e10.ckpt"
    sovits = tmp_path / "x_e10_s100_l32.pth"
    ref = tmp_path / "ref.wav"
    for p in (gpt, sovits, ref):
        p.write_bytes(b"fake")
    return {
        "gpt_weights_path": str(gpt),
        "sovits_weights_path": str(sovits),
        "ref_audio_path": str(ref),
    }


def test_create_list_and_delete_voice(client, tmp_path):
    files = _fake_voice_files(tmp_path / "v1")
    created = client.post(
        "/api/voices",
        json={"name": "测试音色", "prompt_text": "你好呀", **files},
    )
    assert created.status_code == 201, created.text
    voice_id = created.json()["id"]
    assert created.json()["files_ok"] is True
    assert created.json()["used_by"] == 0

    assert [v["name"] for v in client.get("/api/voices").json()["voices"]] == ["测试音色"]

    assert client.delete(f"/api/voices/{voice_id}").status_code == 204
    assert client.get("/api/voices").json()["voices"] == []


def test_create_rejects_missing_files(client, tmp_path):
    files = _fake_voice_files(tmp_path / "v2")
    files["ref_audio_path"] = str(tmp_path / "v2" / "不存在.wav")
    result = client.post("/api/voices", json={"name": "x", "prompt_text": "y", **files})
    assert result.status_code == 400
    assert result.json()["error"]["code"] == "VOICE_FILE_MISSING"


def test_deleting_voice_unassigns_friends_instead_of_cascade(client, tmp_path, monkeypatch):
    """删音色 → 朋友**还在**，只是 voice_id 置空（大坑，专门盯）。"""
    files = _fake_voice_files(tmp_path / "v3")
    voice_id = client.post("/api/voices", json={"name": "阿月的声音", "prompt_text": "你好", **files}).json()["id"]
    friend_id = client.post("/api/friends", json={"name": "阿月", "personality": "话不多"}).json()["id"]

    with db.session_scope() as s:
        s.get(Friend, friend_id).voice_id = voice_id

    assert client.delete(f"/api/voices/{voice_id}").status_code == 204
    with db.session_scope() as s:
        friend = s.get(Friend, friend_id)
        assert friend is not None, "朋友不能被连坐删掉"
        assert friend.voice_id is None, "voice_id 应该被置空"


def test_assign_and_clear_friend_voice(client, tmp_path):
    """给朋友配音色 / 取消配音色（空字符串 = 取消；乱填的 id 要给 4xx）。"""
    files = _fake_voice_files(tmp_path / "v5")
    voice_id = client.post("/api/voices", json={"name": "阿月的声音", "prompt_text": "你好", **files}).json()["id"]
    friend_id = client.post("/api/friends", json={"name": "阿月", "personality": "话不多"}).json()["id"]

    # 默认：没配（不发语音）
    assert client.get(f"/api/friends/{friend_id}").json()["voice_id"] is None

    body = client.patch(f"/api/friends/{friend_id}", json={"voice_id": voice_id}).json()
    assert body["voice_id"] == voice_id

    # 传空字符串 = 取消
    body = client.patch(f"/api/friends/{friend_id}", json={"voice_id": ""}).json()
    assert body["voice_id"] is None

    # 乱填的 id：要 4xx（不能靠外键炸成 500）
    bad = client.patch(f"/api/friends/{friend_id}", json={"voice_id": "不存在"})
    assert bad.status_code == 404
    assert bad.json()["error"]["code"] == "VOICE_NOT_FOUND"


def test_scan_finds_standard_layout_and_reads_prompt_from_filename(client, tmp_path):
    """造一份"标准结构"，看扫描能不能认出音色 + 从文件名取台词。"""
    root = tmp_path / "GPT-SoVITS"
    voice_dir = root / "models" / "v4" / "派蒙" / "v4" / "派蒙_ZH"
    ref_dir = voice_dir / "reference_audios" / "中文" / "emotions"
    ref_dir.mkdir(parents=True)
    (voice_dir / "派蒙_ZH-e10.ckpt").write_bytes(b"x")
    (voice_dir / "派蒙_ZH_e10_s1770_l32.pth").write_bytes(b"x")
    (voice_dir / "派蒙_ZH_e12_s2000_l32.pth").write_bytes(b"x")
    (ref_dir / "【默认】既然差的石头只能做出次品颜料。.wav").write_bytes(b"x")
    # 这种不是台词，必须跳过
    (ref_dir / "ref_随便录的.wav").write_bytes(b"x")

    with db.session_scope() as s:
        from backend.services import settings_service

        settings_service.merge_purposes(s, {"tts": {"sovits_dir": str(root)}})

    body = client.post("/api/voices/scan").json()
    assert body["ok"] is True, body
    names = [c["name"] for c in body["candidates"]]
    assert names == ["派蒙"], names
    candidate = body["candidates"][0]
    assert candidate["prompt_text"] == "既然差的石头只能做出次品颜料。"
    assert candidate["sovits_weights_path"].endswith("派蒙_ZH_e12_s2000_l32.pth"), "应该挑 epoch 最大的"
    assert candidate["already_imported"] is False


def test_scan_without_dir_returns_human_hint(client):
    body = client.post("/api/voices/scan").json()
    assert body["ok"] is False
    assert "目录" in body["hint"] or "没填" in body["hint"]


def test_scan_with_draft_dir_saves_it_and_finds_voices(client, tmp_path):
    """填了草稿目录 → 扫描能找到 + **顺手把目录记下来**（不用先按保存）。"""
    root = tmp_path / "GPT-SoVITS"
    voice_dir = root / "models" / "v4" / "阿月" / "v4" / "阿月_ZH"
    ref_dir = voice_dir / "reference_audios"
    ref_dir.mkdir(parents=True)
    (voice_dir / "阿月_ZH-e10.ckpt").write_bytes(b"x")
    (voice_dir / "阿月_ZH_e10_s100_l32.pth").write_bytes(b"x")
    (ref_dir / "你好呀，我是阿月。.wav").write_bytes(b"x")

    # 注意：**没有**先 PATCH 设置，直接把草稿目录传给 scan
    body = client.post("/api/voices/scan", json={"sovits_dir": str(root)}).json()

    assert body["ok"] is True, body
    assert [c["name"] for c in body["candidates"]] == ["阿月"]
    assert body["saved_dir"] is True
    # 再扫一次（不带草稿）也能找到 —— 说明目录真的存进去了
    again = client.post("/api/voices/scan").json()
    assert again["ok"] is True
    assert len(again["candidates"]) == 1


def test_test_endpoint_for_tts_reports_service_state(client, monkeypatch):
    """模型页那个「测试」按到 TTS 上时，测的是**语音服务在不在**（以前只会说"下一步做"）。"""
    import httpx

    class _Broken:
        def get(self, *_a, **_k):
            raise httpx.ConnectError("refused")

    monkeypatch.setattr(tts_service, "_http", lambda: _Broken())
    tts_service.reset_cache()
    body = client.post("/api/settings/test", json={"purpose": "tts"}).json()
    assert body["ok"] is False
    assert "没开着" in body["error"]


def test_tts_status_says_not_running_in_human_words(client, monkeypatch):
    """连不上时要说人话。

    ⚠️ 这里**故意把 http 打成坏的**，不能假设"这台机器上没开 GPT-SoVITS"——
    开发时它常常是开着的，那样测试就会莫名其妙地红（踩过一次）。
    """
    import httpx

    class _Broken:
        def get(self, *_a, **_k):
            raise httpx.ConnectError("connection refused")

    monkeypatch.setattr(tts_service, "_http", lambda: _Broken())
    tts_service.reset_cache()

    body = client.get("/api/tts/status").json()
    assert body["available"] is False
    assert "没开着" in body["error"]
    assert body["sample_steps"] == 8  # 默认 8 步（D78）


def test_tts_settings_defaults_and_clamps():
    # 这条专测"默认值本身" → 临时把真正的默认地址放回来
    # （conftest 的 `_no_real_tts_service` 会把默认地址指到 9 端口，防单测连真服务 —— D179）
    real = tts_service.DEFAULT_BASE_URL
    tts_service.DEFAULT_BASE_URL = "http://127.0.0.1:9880"
    try:
        conf = tts_service.tts_settings({})
        assert conf["base_url"] == "http://127.0.0.1:9880"
        assert conf["sample_steps"] == 8
        assert "voice_mode" not in conf  # D85：发不发语音是朋友自己的开关，不是全局频率档
        # 离谱的步数要被夹住
        assert tts_service.tts_settings({"tts": {"sample_steps": 999}})["sample_steps"] == 64
        assert tts_service.tts_settings({"tts": {"sample_steps": -5}})["sample_steps"] == 1
    finally:
        tts_service.DEFAULT_BASE_URL = real


def test_cache_file_refuses_path_traversal(client):
    assert tts_service.cache_file("../../user_data/yumefriend.db") is None
    assert tts_service.cache_file("x.wav") is None
    assert tts_service.cache_file("a" * 40 + ".wav") is None  # 合法格式但文件不在


class _FakeResponse:
    def __init__(self, status_code: int = 200, content: bytes = b"RIFFfake") -> None:
        self.status_code = status_code
        self.content = content
        self.text = content.decode("utf-8", "replace")


class _FakeHttp:
    """假的本地 TTS 服务：记下所有请求，固定回一段"音频"。"""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict]] = []

    def get(self, url, params=None, timeout=None):  # noqa: ANN001, ARG002
        self.calls.append(("GET", url, params or {}))
        return _FakeResponse(200)

    def post(self, url, json=None):  # noqa: ANN001
        self.calls.append(("POST", url, json or {}))
        return _FakeResponse(200, b"RIFF" + b"\x00" * 16)


def test_synthesize_caches_and_switches_weights_only_once(client, tmp_path, monkeypatch):
    from backend.models import Voice

    files = _fake_voice_files(tmp_path / "v4")
    fake = _FakeHttp()
    monkeypatch.setattr(tts_service, "_http", lambda: fake)

    voice = Voice(
        id="v1",
        name="阿月",
        speed=1.0,
        prompt_text="你好呀，我是阿月。",  # D77：不能空
        created_at="x",
        updated_at="x",
        **files,
    )
    first = tts_service.synthesize(text="你好", voice=voice, base_url="http://127.0.0.1:9")
    second = tts_service.synthesize(text="你好", voice=voice, base_url="http://127.0.0.1:9")

    assert first == second
    posts = [c for c in fake.calls if c[0] == "POST"]
    assert len(posts) == 1, "第二句应该走缓存，不该再打上游"
    # 切权重只切一次（同一对权重不重复切）
    sets = [c for c in fake.calls if "set_" in c[1]]
    assert len(sets) == 2, sets
    # 请求里带上了台词和语速（D77：prompt_text 不能空）
    payload = posts[0][2]
    assert payload["prompt_text"] and payload["speed_factor"] == 1.0
    assert payload["sample_steps"] == 8


def test_voice_is_committed_before_prewarm_starts(client, tmp_path, monkeypatch):
    """同一个坑（T2.0-9）：预热任务用新会话按 id 读 Voice 行 → 建音色必须先提交。"""
    from backend.models import Voice
    from backend.services import tts_service

    seen: dict = {}

    def fake_prewarm(voice_id: str, *_args, **_kwargs) -> None:
        with db.session_scope() as session:
            row = session.get(Voice, voice_id)
            seen["found"] = row is not None
            seen["prompt_text"] = row.prompt_text if row else None

    monkeypatch.setattr(tts_service, "spawn_prewarm", fake_prewarm)
    files = _fake_voice_files(tmp_path / "v-commit")
    created = client.post(
        "/api/voices", json={"name": "预热测试", "prompt_text": "你好", **files}
    )
    assert created.status_code == 201, created.text
    assert seen.get("found") is True, "起预热任务时 Voice 行必须已经提交"
    assert seen.get("prompt_text") == "你好"
