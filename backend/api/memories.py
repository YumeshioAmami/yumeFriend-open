"""记忆路由（api.md §3）：记忆浏览界面（D26）和用户手动写入都走这里。

HTTP 层 = 用户身份（`Actor.USER`），对记忆有**完整 CRUD**（宪法附录 B）。
LLM 那条路不走 HTTP（api.md §0.3），在 `backend/agent/tools.py` 里。
"""

from fastapi import APIRouter, Depends, Query, Response
from sqlalchemy.orm import Session

from backend.api.deps import get_db
from backend.schemas.memory import MemoryCreate, MemoryUpdate
from backend.services import friend_service, memory_service
from backend.services.actor import Actor

router = APIRouter(prefix="/api", tags=["memories"])


@router.get("/friends/{friend_id}/memories")
def list_memories(
    friend_id: str,
    layer: str | None = Query(default=None),
    tag: str | None = Query(default=None),
    search: str | None = Query(default=None, description="按内容模糊匹配"),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    sort: str = Query(default="recent", description="recent / importance / created"),
    session: Session = Depends(get_db),
) -> dict:
    """记忆浏览界面用（api.md §3.1）。按 layer / tag / 关键词筛选，带总数。"""
    friend_service.get(session, Actor.USER, friend_id)  # 朋友不存在 → 404
    if sort not in memory_service.SORT_OPTIONS:
        raise ValueError(f"sort 只支持 {memory_service.SORT_OPTIONS}，收到 {sort!r}")
    memories, total = memory_service.search(
        session,
        friend_id,
        layer=layer,
        tag=tag,
        text=search,
        limit=limit,
        offset=offset,
        sort=sort,
    )
    return {
        "memories": [memory_service.to_dict(m) for m in memories],
        "total": total,
        # 各层条数：界面上做 tab 角标（几百条时靠它看清分布）
        "layer_counts": memory_service.layer_counts(session, friend_id),
    }


@router.post("/memories")
def create_memory(body: MemoryCreate, response: Response, session: Session = Depends(get_db)) -> dict:
    """用户手动新增（api.md §3.2）。

    同样走**写入去重**（D28）：命中已有那条就返回它，并标记 `merged: true`，
    HTTP 状态用 200（而不是 201）—— 前端据此提示"已合并到已有记忆"。
    """
    friend_service.get(session, Actor.USER, body.friend_id)
    memory, merged = memory_service.save(
        session,
        Actor.USER,
        body.friend_id,
        content=body.content,
        tag=body.tag,
        layer=body.layer,
        context=body.context,
        importance=body.importance,
    )
    response.status_code = 200 if merged else 201
    return {"id": memory.id, "merged": merged}


@router.patch("/memories/{memory_id}")
def update_memory(
    memory_id: str, body: MemoryUpdate, session: Session = Depends(get_db)
) -> dict:
    """改内容 / 重要度 / **layer（升·降级，D230）**；`tag` 不可改（api.md §3.3）。"""
    memory = memory_service.update(
        session,
        Actor.USER,
        memory_id,
        content=body.content,
        importance=body.importance,
        layer=body.layer,
    )
    return memory_service.to_dict(memory)


@router.get("/friends/{friend_id}/memory-events")
def list_memory_events(
    friend_id: str,
    limit: int = Query(default=50, ge=1, le=200),
    session: Session = Depends(get_db),
) -> dict:
    """朋友 / 系统 / 用户动过记忆的留痕（api.md §3.4 / D230），新 → 旧。"""
    friend_service.get(session, Actor.USER, friend_id)  # 朋友不存在 → 404
    events = memory_service.list_events(session, friend_id, limit=limit)
    return {"events": [memory_service.event_to_dict(event) for event in events]}


@router.delete(
    "/memories/{memory_id}",
    status_code=204,
    response_class=Response,
    response_model=None,
)
def delete_memory(memory_id: str, session: Session = Depends(get_db)) -> Response:
    """软删（api.md §3.3）。"""
    memory_service.delete(session, Actor.USER, memory_id)
    return Response(status_code=204)
