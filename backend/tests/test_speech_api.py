"""T2.1-2 语音输入 + 服务启停（D96）。

全部用假实现：**不真的起进程**（起 GPT-SoVITS 要一分钟、还要占显存）。
"""

from __future__ import annotations

import pathlib

import pytest
from fastapi.testclient import TestClient

import backend.config as config
import backend.db as db
from backend.app import create_app
from backend.services import speech_service as sp


@pytest.fixture(autouse=True)
def _clean_procs():
    sp._PROCS.clear()
    sp._HEALTH_CACHE.clear()  # 健康检查有短缓存，别在用例之间串味
    yield
    sp._PROCS.clear()
    sp._HEALTH_CACHE.clear()


@pytest.fixture()
def client(tmp_path, monkeypatch):
    db_file = tmp_path / "speech.db"
    monkeypatch.setattr(config, "DB_PATH", db_file)
    monkeypatch.setattr(db, "DB_PATH", db_file)
    monkeypatch.setattr(db, "_engine", None)
    monkeypatch.setattr(db, "_session_factory", None)
    app = create_app()
    with TestClient(app) as c:
        yield c


def _configure_dir(client, monkeypatch, tmp_path, *, with_python: bool = True) -> pathlib.Path:
    """造一个假的 GPT-SoVITS 目录，并写进设置。"""
    sovits = tmp_path / "GPT-SoVITS"
    (sovits / "runtime").mkdir(parents=True, exist_ok=True)
    if with_python:
        (sovits / "runtime" / "python.exe").write_bytes(b"fake")
    client.patch(
        "/api/settings",
        json={"purposes": {"tts": {"sovits_dir": str(sovits), "base_url": "http://127.0.0.1:9880"}}},
    )
    return sovits


class _FakeProc:
    def __init__(self, *args, **kwargs) -> None:
        self.args = args
        self.kwargs = kwargs
        self.pid = 4321
        self._returncode = None

    def poll(self):
        return self._returncode

    def wait(self, timeout=None):  # noqa: ANN001
        self._returncode = 0
        return 0


# ---------- 状态 / 启停 ----------


def test_status_without_sovits_dir_says_what_to_do(client, monkeypatch):
    monkeypatch.setattr(sp, "_health", lambda _name: False)
    body = client.get("/api/speech/services").json()
    assert set(body) == {"tts", "stt"}
    assert body["stt"]["state"] == "stopped"
    assert "GPT-SoVITS 的程序目录" in body["tts"]["hint"]


def test_start_without_dir_is_a_human_hint(client, monkeypatch):
    monkeypatch.setattr(sp, "_health", lambda _name: False)
    body = client.post("/api/speech/services/stt/start").json()
    assert body["ok"] is False
    assert "程序目录" in body["hint"]


def test_start_does_not_spawn_when_already_running(client, monkeypatch, tmp_path):
    """已经开着的（含用户自己双击 .bat 开的）→ 只认状态，**不重复起进程**。"""
    _configure_dir(client, monkeypatch, tmp_path)
    monkeypatch.setattr(sp, "_health", lambda _name: True)

    def _boom(*_a, **_k):
        raise AssertionError("不该起新进程")

    monkeypatch.setattr(sp.subprocess, "Popen", _boom)
    body = client.post("/api/speech/services/tts/start").json()
    assert body["ok"] is True and body["state"] == "running"
    assert "已经开着" in body["hint"]


def test_spawn_builds_the_right_command(client, monkeypatch, tmp_path):
    sovits = _configure_dir(client, monkeypatch, tmp_path)
    monkeypatch.setattr(sp, "_health", lambda _name: False)
    spawned: list[_FakeProc] = []

    def fake_popen(args, **kwargs):  # noqa: ANN001
        proc = _FakeProc(args, **kwargs)
        spawned.append(proc)
        return proc

    monkeypatch.setattr(sp.subprocess, "Popen", fake_popen)
    assert client.post("/api/speech/services/stt/start").json()["state"] == "starting"
    assert client.post("/api/speech/services/tts/start").json()["state"] == "starting"

    stt_args = spawned[0].args[0]
    assert stt_args[0].endswith("runtime\\python.exe") or stt_args[0].endswith("runtime/python.exe")
    assert "stt_server.py" in stt_args[1]
    assert "--port" in stt_args and "9881" in stt_args
    assert spawned[0].kwargs["cwd"] == str(sovits)

    tts_args = spawned[1].args[0]
    assert "api_v2.py" in tts_args[1]
    assert "9880" in tts_args


def test_state_machine_starting_then_running(client, monkeypatch, tmp_path):
    _configure_dir(client, monkeypatch, tmp_path)
    monkeypatch.setattr(sp, "_health", lambda _name: False)
    monkeypatch.setattr(sp.subprocess, "Popen", lambda *a, **k: _FakeProc())
    client.post("/api/speech/services/stt/start")
    assert sp.state("stt") == "starting"
    monkeypatch.setattr(sp, "_health", lambda _name: True)
    assert sp.state("stt") == "running"


def test_stop_refuses_to_kill_someone_elses_service(client, monkeypatch):
    monkeypatch.setattr(sp, "_health", lambda _name: True)  # 有人开着，但不是我们起的
    body = client.post("/api/speech/services/tts/stop").json()
    assert body["ok"] is False
    assert "不是 yumeFriend 启动的" in body["hint"]


def test_stop_kills_our_own_process_tree(client, monkeypatch, tmp_path):
    _configure_dir(client, monkeypatch, tmp_path)
    monkeypatch.setattr(sp, "_health", lambda _name: False)
    monkeypatch.setattr(sp.subprocess, "Popen", lambda *a, **k: _FakeProc())
    client.post("/api/speech/services/stt/start")

    calls: list[list[str]] = []
    monkeypatch.setattr(sp.subprocess, "run", lambda args, **k: calls.append(list(args)))
    body = client.post("/api/speech/services/stt/stop").json()
    assert body["ok"] is True
    assert calls and calls[0][:1] == ["taskkill"] and "/T" in calls[0], "要连子进程一起杀（D67）"
    assert "stt" not in sp._PROCS


def test_stop_all_only_touches_ours(monkeypatch):
    stopped: list[str] = []
    monkeypatch.setattr(sp, "_PROCS", {"stt": {"proc": _FakeProc(), "started_at": 0.0}})
    monkeypatch.setattr(sp, "stop", lambda name: stopped.append(name) or {"ok": True})
    sp.stop_all_ours()
    assert stopped == ["stt"]


# ---------- 转写 ----------


class _FakeHttp:
    """假的 STT 服务客户端。"""

    def __init__(self, payload: dict) -> None:
        self._payload = payload
        self.posts: list[dict] = []

    def __enter__(self):
        return self

    def __exit__(self, *_a):
        return False

    def post(self, url, content=None, headers=None):  # noqa: ANN001
        self.posts.append({"url": url, "content": content})

        class _Resp:
            status_code = 200

            def __init__(self, payload: dict) -> None:
                self._payload = payload

            def json(self):
                return self._payload

        return _Resp(self._payload)


def _prep_transcribe(client, monkeypatch, tmp_path, payload: dict):
    _configure_dir(client, monkeypatch, tmp_path)
    monkeypatch.setattr(sp, "_health", lambda name: name == "stt")
    wav = tmp_path / "conv.16k.wav"
    wav.write_bytes(b"RIFFfake")
    monkeypatch.setattr(sp, "_to_wav", lambda *_a, **_k: wav)
    fake = _FakeHttp(payload)
    monkeypatch.setattr(sp.httpx, "Client", lambda *a, **k: fake)
    return fake


def test_transcribe_returns_text(client, monkeypatch, tmp_path):
    fake = _prep_transcribe(client, monkeypatch, tmp_path, {"ok": True, "text": " 你好呀 "})
    body = client.post(
        "/api/speech/transcribe", files={"file": ("rec.webm", b"webm-bytes", "audio/webm")}
    ).json()
    assert body == {"ok": True, "text": "你好呀"}
    assert fake.posts and fake.posts[0]["url"].endswith(":9881/asr")
    assert fake.posts[0]["content"] == b"RIFFfake", "喂给 STT 的应该是转好的 wav"


def test_transcribe_failure_is_a_human_hint(client, monkeypatch, tmp_path):
    _prep_transcribe(client, monkeypatch, tmp_path, {"ok": False, "error": "模型炸了"})
    body = client.post(
        "/api/speech/transcribe", files={"file": ("rec.webm", b"x", "audio/webm")}
    ).json()
    assert body["ok"] is False
    assert "识别失败" in body["hint"] and "模型炸了" in body["hint"]


def test_transcribe_without_dir_is_a_hint(client):
    body = client.post(
        "/api/speech/transcribe", files={"file": ("rec.webm", b"x", "audio/webm")}
    ).json()
    assert body["ok"] is False and "程序目录" in body["hint"]


def test_to_wav_uses_the_bundled_ffmpeg(monkeypatch, tmp_path):
    sovits = tmp_path / "GPT"
    sovits.mkdir()
    (sovits / "ffmpeg.exe").write_bytes(b"fake")
    calls: list[list[str]] = []

    class _Done:
        returncode = 0
        stderr = b""

    def fake_run(args, **kwargs):  # noqa: ANN001
        calls.append(list(args))
        pathlib.Path(args[-1]).write_bytes(b"RIFFconverted")
        return _Done()

    monkeypatch.setattr(sp.subprocess, "run", fake_run)
    out = sp._to_wav(str(sovits), b"webm", "rec.webm")
    assert out is not None and out.read_bytes() == b"RIFFconverted"
    assert str(sovits / "ffmpeg.exe") in calls[0][0]
    assert "-ar" in calls[0] and "16000" in calls[0]
    out.unlink()


def test_desktop_shutdown_reaps_our_speech_services(monkeypatch):
    """T2.1-2 抓到的坑：外壳退出走 `os._exit`，lifespan 收尾**不会跑** ——
    所以 `shutdown()` 必须自己把我们起的语音服务收掉（不然留孤儿占端口）。"""
    from backend.desktop import app as desktop_app

    called: list[str] = []
    monkeypatch.setattr(sp, "stop_all_ours", lambda: called.append("stop"))
    monkeypatch.setattr(desktop_app.speech_service, "stop_all_ours", lambda: called.append("stop"))
    monkeypatch.setattr(desktop_app.os, "_exit", lambda _code: None)
    monkeypatch.setattr(desktop_app.time, "sleep", lambda _s: None)
    desktop_app.shutdown()
    assert called == ["stop"]


# ---------- 孤儿清理（父进程被硬杀时留下的）----------

def test_spawn_writes_pid_record_and_stop_clears_it(client, monkeypatch, tmp_path):
    _configure_dir(client, monkeypatch, tmp_path)
    monkeypatch.setattr(
        sp, "_PID_FILE", tmp_path / "speech_services.json"
    )
    monkeypatch.setattr(sp, "_health", lambda _name: False)
    monkeypatch.setattr(sp.subprocess, "Popen", lambda *a, **k: _FakeProc())
    client.post("/api/speech/services/stt/start")
    assert (tmp_path / "speech_services.json").exists()
    monkeypatch.setattr(sp.subprocess, "run", lambda *a, **k: None)
    client.post("/api/speech/services/stt/stop")
    assert (tmp_path / "speech_services.json").read_text(encoding="utf-8") == "{}"


def test_cleanup_leftovers_only_kills_our_own_command_line(monkeypatch, tmp_path):
    """孤儿清理：**只杀命令行里带 api_v2.py / stt_server.py 的**（PID 会被系统回收，别误杀）。"""
    pid_file = tmp_path / "speech_services.json"
    pid_file.write_text('{"stt": 111, "tts": 222}', encoding="utf-8")
    monkeypatch.setattr(sp, "_PID_FILE", pid_file)
    monkeypatch.setattr(
        sp,
        "_pid_command_line",
        lambda pid: {
            111: r"D:\GPT-SoVITS\runtime\python.exe E:\...\backend\speech\stt_server.py --port 9881",
            222: r"C:\Windows\notepad.exe",   # PID 被回收成别的程序了 → 不许动
        }.get(pid, ""),
    )
    killed_cmds: list[list[str]] = []
    monkeypatch.setattr(sp.subprocess, "run", lambda args, **k: killed_cmds.append(list(args)))
    assert sp.cleanup_leftovers() == ["stt"]
    assert killed_cmds and killed_cmds[0][1:2] == ["/PID"] and "111" in killed_cmds[0]
    assert not pid_file.exists(), "收完要把记录删掉"


def test_cleanup_leftovers_is_quiet_when_nothing_recorded(monkeypatch, tmp_path):
    monkeypatch.setattr(sp, "_PID_FILE", tmp_path / "nope.json")
    assert sp.cleanup_leftovers() == []
