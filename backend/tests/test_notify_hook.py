"""通知钩子（`backend/notify.py`）：登记 / 广播 / **炸了也不能往外抛**。

这几条要成立，否则"弹窗"这种锦上添花的东西就能把聊天搞挂。
"""

from __future__ import annotations

import pytest

from backend import notify


@pytest.fixture(autouse=True)
def _clean_hooks():
    notify.clear()
    yield
    notify.clear()


def test_emit_calls_registered_handler():
    seen: list[dict] = []
    notify.register(seen.append)

    notify.emit({"kind": "proactive", "friend_name": "小敏"})

    assert seen == [{"kind": "proactive", "friend_name": "小敏"}]


def test_emit_without_handler_is_silent():
    notify.emit({"kind": "proactive"})  # 不该报错（开发模式只跑 backend.main）


def test_unregister_stops_delivery():
    seen: list[dict] = []
    notify.register(seen.append)
    notify.unregister(seen.append)

    notify.emit({"kind": "proactive"})

    assert seen == []


def test_register_twice_only_fires_once():
    seen: list[dict] = []
    notify.register(seen.append)
    notify.register(seen.append)

    notify.emit({"kind": "proactive"})

    assert len(seen) == 1


def test_broken_handler_does_not_raise_nor_block_others():
    seen: list[dict] = []

    def boom(_event: dict) -> None:
        raise RuntimeError("弹窗炸了")

    notify.register(boom)
    notify.register(seen.append)

    notify.emit({"kind": "proactive"})  # 不许抛

    assert len(seen) == 1  # 后面的 handler 照样收到
