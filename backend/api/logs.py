"""日志路由（api.md §6）：给日志窗口读实时日志 + 一键诊断。"""

from fastapi import APIRouter, Depends, Query, Response
from sqlalchemy.orm import Session

from backend import logbuffer
from backend.api.deps import get_db
from backend.config import APP_NAME, APP_VERSION
from backend.services import settings_service

router = APIRouter(prefix="/api/logs", tags=["logs"])


@router.get("")
def read_logs(
    since: int = Query(default=0, ge=0, description="上次拿到的最大 seq"),
    level: str | None = Query(default=None, description="DEBUG/INFO/WARNING/ERROR"),
    limit: int = Query(default=500, ge=1, le=2000),
) -> dict:
    """增量读（api.md §6）。前端每 2 秒拉一次。"""
    if level and level.upper() not in logbuffer.LEVELS:
        raise ValueError(f"不认识的级别：{level}")
    items, next_seq = logbuffer.snapshot(since=since, level=level, limit=limit)
    return {"items": items, "next_seq": next_seq, "size": logbuffer.size()}


@router.delete("", status_code=204, response_class=Response, response_model=None)
def clear_logs() -> Response:
    logbuffer.clear()
    return Response(status_code=204)


@router.get("/diagnostics")
def diagnostics(session: Session = Depends(get_db)) -> dict:
    """一键复制的诊断信息（D31 第 4 条 / frontend.md §5.3）。

    目的是"贴给 Codex 就能定位问题"，省掉一轮"你再描述一下"。
    """
    bases, active = settings_service.llm_config(session)
    row = settings_service.get(session)
    try:
        import json

        cfg = json.loads(bases or "[]")
        current = next((b for b in cfg if b.get("name") == active), cfg[0] if cfg else {})
    except Exception:  # noqa: BLE001
        current = {}

    lines = [
        f"{APP_NAME} v{APP_VERSION} | provider={active or '(未配置)'} | "
        f"model={current.get('model') or '(未配置)'}",
        f"proxy={current.get('proxy') or '(跟随系统)'} | 频率={row.active_message_frequency} 分钟 | "
        f"深度思考={'开' if row.deep_thinking else '关'}",
    ]
    errors = logbuffer.recent_errors(limit=1)
    if errors:
        last = errors[0]
        lines.append(f"最近错误：{last['time']} [{last['level']}] {last['message']}")
    else:
        lines.append("最近错误：无")

    lines.append("")
    lines.append(f"最近日志（{len(logbuffer.tail(20))} 条）：")
    for entry in logbuffer.tail(20):
        lines.append(f"  {entry['time']} [{entry['level']}] {entry['where']} {entry['message']}")

    return {"text": "\n".join(lines)}
