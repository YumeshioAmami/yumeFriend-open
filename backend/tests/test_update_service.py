"""D456：桌面自动更新的检查 / 安全解压 / updater 目录交换。"""

from __future__ import annotations

import importlib.util
import hashlib
import time
import zipfile
from pathlib import Path

import pytest

from backend.services import update_service


ROOT = Path(__file__).resolve().parents[2]


def _load_updater():
    path = ROOT / "scripts" / "updater_entry.py"
    spec = importlib.util.spec_from_file_location("yume_updater_entry", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_update_check_detects_newer_version(monkeypatch):
    monkeypatch.setattr(update_service.config, "APP_VERSION", "1.0.0")
    monkeypatch.setattr(update_service, "_supported", lambda: True)
    monkeypatch.setattr(update_service, "_writable_install_dir", lambda: True)
    monkeypatch.setattr(
        update_service,
        "_fetch_manifest",
        lambda *, force=False: {
            "version": "1.2.0",
            "desktop_zip_url": "https://example.com/app.zip",
            "desktop_sha256": "abc",
            "desktop_size": 123,
            "notes": "更新说明",
        },
    )
    info = update_service.check(force=True)
    assert info["update_available"] is True
    assert info["latest_version"] == "1.2.0"
    assert info["can_auto_update"] is True


def test_safe_extract_rejects_traversal(tmp_path):
    archive = tmp_path / "bad.zip"
    with zipfile.ZipFile(archive, "w") as bundle:
        bundle.writestr("../evil.txt", "x")
    with pytest.raises(ValueError):
        update_service._safe_extract(archive, tmp_path / "out")


def test_update_history_is_newest_first_and_tolerates_bad_lines(tmp_path, monkeypatch):
    logs = tmp_path / "logs"
    logs.mkdir()
    monkeypatch.setattr(update_service.config, "LOG_DIR", logs)
    update_service._append_history("downloaded", version="1.2.0", from_version="1.1.0")
    update_service._append_history(
        "failed",
        version="1.2.0",
        from_version="1.1.0",
        message="sha mismatch",
    )
    with (logs / "update_history.jsonl").open("a", encoding="utf-8") as handle:
        handle.write("not-json\n")
    items = update_service.history()
    assert [item["event"] for item in items] == ["failed", "downloaded"]
    assert items[0]["message"] == "sha mismatch"


def test_download_prepares_staging_folder(tmp_path, monkeypatch):
    install = tmp_path / "install"
    install.mkdir()
    archive = tmp_path / "update.zip"
    with zipfile.ZipFile(archive, "w") as bundle:
        bundle.writestr("yumeFriend.exe", b"new-exe")
        bundle.writestr("yumeFriend-updater.exe", b"updater")
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    monkeypatch.setattr(update_service.config, "APP_VERSION", "1.0.0")
    monkeypatch.setattr(update_service.config, "LOG_DIR", tmp_path / "logs")
    monkeypatch.setattr(update_service, "_install_dir", lambda: install)
    monkeypatch.setattr(update_service, "_supported", lambda: True)
    monkeypatch.setattr(update_service, "_writable_install_dir", lambda: True)
    monkeypatch.setattr(
        update_service,
        "_fetch_manifest",
        lambda *, force=False: {
            "version": "9.9.9",
            "desktop_zip_url": archive.as_uri(),
            "desktop_sha256": digest,
            "desktop_size": archive.stat().st_size,
            "notes": "",
        },
    )
    monkeypatch.setattr(
        update_service,
        "_state",
        {
            "state": "idle",
            "progress": 0.0,
            "downloaded_bytes": 0,
            "total_bytes": 0,
            "error": None,
            "updated_at": None,
            "staging_dir": None,
        },
    )
    monkeypatch.setattr(update_service, "_thread", None)
    update_service.start_download()
    deadline = time.time() + 10
    while time.time() < deadline:
        status = update_service.check(force=False)
        if status["download"]["state"] in {"ready", "error"}:
            break
        time.sleep(0.05)
    status = update_service.check(force=False)
    assert status["download"]["state"] == "ready", status
    staging = Path(status["download"]["staging_dir"]) / "app"
    assert (staging / "yumeFriend.exe").read_bytes() == b"new-exe"
    history = update_service.history()
    assert history[0]["event"] == "downloaded"
    assert history[0]["version"] == "9.9.9"


def test_updater_swaps_directory_and_keeps_user_data(tmp_path, monkeypatch):
    updater = _load_updater()
    install = tmp_path / "yumeFriend"
    staging = tmp_path / ".yumeFriend-update" / "1.2.0" / "app"
    install.mkdir()
    staging.mkdir(parents=True)
    (install / "yumeFriend.exe").write_bytes(b"old")
    (install / "user_data").mkdir()
    (install / "user_data" / "note.txt").write_text("chat", encoding="utf-8")
    (staging / "yumeFriend.exe").write_bytes(b"new")
    (staging / "yumeFriend-updater.exe").write_bytes(b"updater")

    class FakeProc:
        pid = 1

        def poll(self):
            return None

    monkeypatch.setattr(updater, "_wait_for_process", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(updater, "_relaunch", lambda *_args, **_kwargs: FakeProc())
    monkeypatch.setattr(updater.time, "sleep", lambda *_args, **_kwargs: None)

    code = updater.apply_update(
        pid=123,
        install_dir=install,
        staging_dir=staging,
        relaunch_path=install / "yumeFriend.exe",
        log_file=tmp_path / "updater.log",
        history_file=tmp_path / "update_history.jsonl",
        version="1.2.0",
        from_version="1.1.0",
        settle_seconds=0,
    )
    assert code == 0
    assert (install / "yumeFriend.exe").read_bytes() == b"new"
    assert (install / "user_data" / "note.txt").read_text(encoding="utf-8") == "chat"
    assert '"event": "applied"' in (tmp_path / "update_history.jsonl").read_text(encoding="utf-8")


def test_updater_rolls_back_when_new_app_exits(tmp_path, monkeypatch):
    updater = _load_updater()
    install = tmp_path / "yumeFriend"
    staging = tmp_path / ".yumeFriend-update" / "1.2.0" / "app"
    install.mkdir()
    staging.mkdir(parents=True)
    (install / "yumeFriend.exe").write_bytes(b"old")
    (install / "user_data").mkdir()
    (install / "user_data" / "note.txt").write_text("chat", encoding="utf-8")
    (staging / "yumeFriend.exe").write_bytes(b"new")
    (staging / "yumeFriend-updater.exe").write_bytes(b"updater")

    class DeadProc:
        pid = 1

        def poll(self):
            return 1

    monkeypatch.setattr(updater, "_wait_for_process", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(updater, "_relaunch", lambda *_args, **_kwargs: DeadProc())
    monkeypatch.setattr(updater.time, "sleep", lambda *_args, **_kwargs: None)

    code = updater.apply_update(
        pid=123,
        install_dir=install,
        staging_dir=staging,
        relaunch_path=install / "yumeFriend.exe",
        log_file=tmp_path / "updater.log",
        history_file=tmp_path / "update_history.jsonl",
        version="1.2.0",
        from_version="1.1.0",
        settle_seconds=0,
    )
    assert code == 1
    assert (install / "yumeFriend.exe").read_bytes() == b"old"
    assert (install / "user_data" / "note.txt").read_text(encoding="utf-8") == "chat"
    assert '"event": "failed"' in (tmp_path / "update_history.jsonl").read_text(encoding="utf-8")
