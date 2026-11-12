"""手机本地通知桥：Python → Kotlin NotificationBridge。"""

from __future__ import annotations


def emit(event: dict) -> None:
    try:
        from java import jclass  # noqa: PLC0415

        bridge = jclass("com.yumefriend.app.NotificationBridge")
        bridge.notify(
            str(event.get("friend_id") or ""),
            str(event.get("friend_name") or "yumeFriend"),
            str(event.get("text") or ""),
        )
    except Exception:  # noqa: BLE001
        # 通知失败绝不影响消息落库。
        pass
