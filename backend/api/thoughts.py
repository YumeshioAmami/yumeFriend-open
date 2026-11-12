"""小纸条路由（D192）：让用户能随时看到"她攒着想说的那件事"。

只做两件事：**看**（待说 + 待回）和**划掉**（作废）。她怎么用纸条是 `thought_service` 的事。
"""

from fastapi import APIRouter, Depends, Response
from sqlalchemy.orm import Session

from backend.api.deps import get_db
from backend.services import friend_service, thought_service
from backend.services.actor import Actor

router = APIRouter(prefix="/api", tags=["thoughts"])


@router.get("/friends/{friend_id}/thoughts")
def list_thoughts(friend_id: str, session: Session = Depends(get_db)) -> dict:
    """她手里还惦记着的纸条（待说 + 待回）+ 各状态条数。"""
    friend_service.get(session, Actor.USER, friend_id)  # 朋友不存在 → 404
    return thought_service.list_for_ui(session, friend_id)


@router.delete(
    "/thoughts/{thought_id}",
    status_code=204,
    response_class=Response,
    response_model=None,
)
def drop_thought(thought_id: str, session: Session = Depends(get_db)) -> Response:
    """划掉一张纸条（= 作废，她不占惦记了）。"""
    thought_service.drop(session, thought_id)
    return Response(status_code=204)
