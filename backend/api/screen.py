"""看屏的 focus 端点（v2.1 / T2.1-5 / D100 + v2.2 / D103）。

手动看屏不走这里 —— 它是"带屏幕截图的普通回合"（`POST /messages {screen: true}`）。
这里只管"前端告诉我现在开着谁"。

⚠️ v2.2（D103）之后，**看屏不再由 focus 决定**：看屏变成"主动消息的第一步"，
谁要说话谁才看。这两个端点先留着（桌宠 Q33 会用到），前端那 60 秒心跳也留着 ——
它不花钱，等桌宠上线就直接能用。
"""

from fastapi import APIRouter
from sqlalchemy.orm import Session
from fastapi import Depends

from backend.api.deps import get_db
from backend.services import screen_service, settings_service

router = APIRouter(prefix="/api/focus", tags=["screen"])


@router.post("")
def set_focus(body: dict) -> dict:
    """前端报"我现在开着谁"（切朋友时 + 每 60 秒心跳；没人开着就传 null）。"""
    friend_id = body.get("friend_id")
    screen_service.set_focus(str(friend_id) if friend_id else None)
    return screen_service.get_focus()


@router.get("")
def read_focus(session: Session = Depends(get_db)) -> dict:
    row = settings_service.get(session)
    state = screen_service.get_focus()
    return {
        **state,
        # D103：开关还在（含义 = "主动消息之前先看一眼"），但不再有"看屏间隔"这个数字
        "enabled": bool(row.screen_watch_enabled),
    }
