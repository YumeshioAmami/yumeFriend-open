"""桌面外壳的「保存图片」。

v3.1：WebView2 **不会自己下载**，所以这条得 Python 落地（取图 → 写盘 → 回真实路径）。
v3.7（D163）：用户要"自己选路径" → 改成弹**原生「另存为」对话框**，写到用户选的文件。
所以这里直接注入一个假的"选路径"回调，把对话框那一步换成测试里给的路径。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from backend.desktop.pet import ShellApi


class _FakeResponse:
    def __init__(self, content: bytes) -> None:
        self.content = content

    def raise_for_status(self) -> None:
        return None


def _api(chosen: str | None, *, seen: list[str] | None = None) -> ShellApi:
    """一个只认"选路径"那一步的 ShellApi（不碰真对话框、不碰真主目录）。"""

    def ask(default_name: str) -> str | None:
        if seen is not None:
            seen.append(default_name)
        return chosen

    return ShellApi(None, ask_save_path=ask)


def test_save_image_writes_where_the_user_chose(tmp_path, monkeypatch):
    monkeypatch.setattr("httpx.get", lambda *a, **k: _FakeResponse(b"\x89PNG\r\n\x1a\nfake"))
    target = tmp_path / "我的图" / "abc123.png"
    seen: list[str] = []

    result = _api(str(target), seen=seen).save_image(
        "http://127.0.0.1:8001/api/images/abc123.png"
    )

    assert result["ok"] is True
    assert result["path"] == str(target)
    assert target.read_bytes().startswith(b"\x89PNG")
    # 默认文件名给的是原图名（保存框里预填它）
    assert seen == ["abc123.png"]


def test_save_image_keeps_the_url_suffix_if_user_typed_none(tmp_path, monkeypatch):
    monkeypatch.setattr("httpx.get", lambda *a, **k: _FakeResponse(b"jpg"))
    target = tmp_path / "随手起的名"

    result = _api(str(target)).save_image("http://x/api/images/a.jpg")

    assert result["ok"] is True
    assert Path(result["path"]).name == "随手起的名.jpg"
    assert Path(result["path"]).read_bytes() == b"jpg"


def test_save_image_cancel_is_human(tmp_path, monkeypatch):
    monkeypatch.setattr("httpx.get", lambda *a, **k: _FakeResponse(b"png"))

    result = _api(None).save_image("http://x/api/images/a.png")

    assert result["ok"] is False
    assert result["error"] == "你取消了"


def test_save_image_failures_are_human(tmp_path, monkeypatch):
    def _boom(*_a, **_k):
        raise OSError("连不上")

    monkeypatch.setattr("httpx.get", _boom)
    assert _api(str(tmp_path / "a.png")).save_image("http://x/a.png")["ok"] is False
    # 取图就失败了 → 根本不该弹对话框（所以这里给个一定会炸的路径都不该被用到）
    assert _api(str(tmp_path / "b.png")).save_image("")["error"] == "没给图片地址"


def test_save_image_without_a_window_is_human(tmp_path, monkeypatch):
    """没交窗口进来（不该发生）→ 也说人话，别抛。"""
    monkeypatch.setattr("httpx.get", lambda *a, **k: _FakeResponse(b"png"))

    result = ShellApi(None).save_image("http://x/api/images/a.png")

    assert result["ok"] is False
    assert result["error"] == "外壳还没准备好（没有主窗口）"


class _FakeWindow:
    """假的 pywebview 主窗口：只记下 `create_file_dialog` 被怎么调的。"""

    def __init__(self, result) -> None:
        self.result = result
        self.calls: list[tuple[tuple, dict]] = []

    def create_file_dialog(self, *args, **kwargs):  # noqa: ANN002, ANN003
        self.calls.append((args, kwargs))
        return self.result


def test_uses_the_native_save_dialog(tmp_path, monkeypatch):
    """真跑外壳时那条路：弹**原生「另存为」**，默认文件名是原图名（D163）。"""
    import webview

    monkeypatch.setattr("httpx.get", lambda *a, **k: _FakeResponse(b"png"))
    chosen = tmp_path / "我自己选的名字.png"
    window = _FakeWindow((str(chosen),))
    api = ShellApi(None)
    api.set_window(window)

    result = api.save_image("http://x/api/images/a.png")

    assert result["ok"] is True
    assert Path(result["path"]) == chosen
    assert chosen.read_bytes() == b"png"
    (args, kwargs), = window.calls
    assert args[0] == webview.FileDialog.SAVE
    assert kwargs["save_filename"] == "a.png"
    assert "图片" in kwargs["file_types"][0]


def test_native_dialog_cancel_is_human(tmp_path, monkeypatch):
    monkeypatch.setattr("httpx.get", lambda *a, **k: _FakeResponse(b"png"))
    api = ShellApi(None)
    api.set_window(_FakeWindow(None))

    result = api.save_image("http://x/api/images/a.png")

    assert result["ok"] is False
    assert result["error"] == "你取消了"
