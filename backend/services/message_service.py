"""消息业务逻辑（api.md §2 / data_model.md §3）。

约定：
· `content` 永远是给用户看的**干净正文**（工具块在 agent 层剥离后才落到这里）。
· `stream_status`：NULL/streaming/completed/error/partial（LLM协议§8）。
· `tools_used`：本轮工具调用记录 JSON（D31）。
· 思考链**不落库**（D23）。
"""

import json
import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from backend.models import Message
from backend.services.errors import NotFoundError

STREAMING = "streaming"
COMPLETED = "completed"
ERROR = "error"
PARTIAL = "partial"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _now_after(session: Session, friend_id: str) -> str:
    """同一个朋友的消息时间戳**必须严格递增**。

    为什么：Windows 上 `datetime.now()` 的粒度可能只有 ~1 毫秒，
    "用户发一句 → 她回一句"经常落在同一个时间戳里；而 `page()` 是按
    `created_at` 排序的 —— 时间戳一样时两条消息的先后就变成**随机的**
    （靠 UUID 大小决定），于是历史里会出现"她的回复排在你的消息前面"。
    这里遇到"不比上一条晚"就把时间往前挪 1 微秒，保证顺序永远确定。
    """
    now = datetime.now(timezone.utc)
    latest = session.execute(
        select(func.max(Message.created_at)).where(Message.friend_id == friend_id)
    ).scalar()
    if latest:
        try:
            previous = datetime.fromisoformat(str(latest).replace("Z", "+00:00"))
        except ValueError:
            previous = None
        if previous is not None:
            if previous.tzinfo is None:
                previous = previous.replace(tzinfo=timezone.utc)
            if now <= previous:
                now = previous + timedelta(microseconds=1)
    return now.isoformat()


def create(
    session: Session,
    friend_id: str,
    *,
    role: str,
    content: str = "",
    mode_at_send: str | None = None,
    is_proactive: bool = False,
    stream_status: str | None = None,
    tools_used: str | None = None,
    image_path: str | None = None,
    image_no: int | None = None,
    mood: str | None = None,
    moods: list[str] | None = None,
    origin_device_id: str | None = None,
) -> Message:
    if origin_device_id is None:
        try:
            # 懒导入，避免 models ↔ services 循环。消息永远标上“谁发的”（D342）。
            from backend.services import sync_service  # noqa: PLC0415

            origin_device_id = sync_service.device_id(session)
        except Exception:  # noqa: BLE001 —— 同步元数据坏了不能拖累聊天
            origin_device_id = ""
    created_at = _now_after(session, friend_id)
    message = Message(
        id=str(uuid.uuid4()),
        friend_id=friend_id,
        role=role,
        content=content,
        mode_at_send=mode_at_send,
        is_proactive=1 if is_proactive else 0,
        stream_status=stream_status,
        tools_used=tools_used,
        image_path=image_path,
        image_no=image_no,
        mood=mood,
        moods=json.dumps(moods, ensure_ascii=False) if moods else None,
        origin_device_id=origin_device_id or None,
        created_at=created_at,
        updated_at=created_at,
    )
    session.add(message)
    session.flush()
    from backend.services import sync_service  # noqa: PLC0415

    sync_service.mark_message_dirty(session, message.id)
    return message


def next_image_no(session: Session, friend_id: str) -> int:
    """这个朋友的**下一张图编号**（从 1 开始）——提示词占位和 `look_image` 都用它（D82）。"""
    current = session.execute(
        select(func.max(Message.image_no)).where(Message.friend_id == friend_id)
    ).scalar()
    return int(current or 0) + 1


def finish(
    session: Session,
    message: Message,
    *,
    content: str,
    stream_status: str,
    tools_used: str | None = None,
) -> Message:
    """一轮生成结束时回填正文与状态（断开 → partial，异常 → error）。"""
    message.content = content
    message.stream_status = stream_status
    message.updated_at = _now()
    if tools_used is not None:
        message.tools_used = tools_used
    session.flush()
    from backend.services import sync_service  # noqa: PLC0415

    sync_service.mark_message_dirty(session, message.id)
    return message


def reset_stale_streaming() -> int:
    """启动时把上次进程留下的 `streaming` 标成 `partial`（v3.14 / D182）。

    为什么：回复现在跑在**后台 task** 里，进程一退出它就跟没了 ——
    库里会留下一条 `stream_status = "streaming"` 的消息。
    因为我们是"边生成边落库"（`_flush_progress`），**能留多少留多少**，
    标成 partial 让前端照常显示（她说到一半被关了）。
    """
    from backend.db import session_scope  # 懒导入，避免循环
    from backend.services import sync_service  # noqa: PLC0415

    with session_scope() as session:
        rows = list(
            session.execute(select(Message).where(Message.stream_status == STREAMING)).scalars()
        )
        for message in rows:
            message.stream_status = PARTIAL
            message.updated_at = _now()
            sync_service.mark_message_dirty(session, message.id)
        return len(rows)


def recent(session: Session, friend_id: str, *, limit: int) -> list[Message]:
    """取最近 N 条，返回**时间正序**（老的在前），正好喂给上下文组装。"""
    stmt = (
        select(Message)
        .where(Message.friend_id == friend_id)
        .order_by(Message.created_at.desc())
        .limit(limit)
    )
    return list(reversed(list(session.execute(stmt).scalars())))


def recent_for_context(session: Session, friend_id: str, *, limit: int) -> list[Message]:
    """喂给上下文的最近 N 条（D43）。

    比 `recent` 多一条过滤：**跳过内容为空的 assistant 消息**。
    生成失败 / 一个字都没出来就断开时，库里会留一条 `content = ""` 的 assistant 记录；
    把它塞进历史，模型会看到一条空回复，容易跟着跑偏。
    有内容的 partial 消息照常带着（用户确实看到过）。
    """
    stmt = (
        select(Message)
        .where(
            Message.friend_id == friend_id,
            or_(Message.role != "assistant", Message.content != ""),
        )
        .order_by(Message.created_at.desc())
        .limit(limit)
    )
    return list(reversed(list(session.execute(stmt).scalars())))


def page(
    session: Session,
    friend_id: str,
    *,
    limit: int = 50,
    before: str | None = None,
) -> tuple[list[Message], bool]:
    """历史分页（api.md §2.2）。

    返回 `(messages, has_more)`；`messages` 按**时间正序**（老的在前，前端直接渲染）。
    `before` 传上一页最老那条的 `created_at`，用来往前翻。
    多取一条来判断还有没有更早的，避免额外 count 查询。
    """
    stmt = select(Message).where(
        Message.friend_id == friend_id,
        or_(Message.role != "assistant", Message.content != ""),
    )
    if before:
        stmt = stmt.where(Message.created_at < before)
    stmt = stmt.order_by(Message.created_at.desc(), Message.id.desc()).limit(limit + 1)
    rows = list(session.execute(stmt).scalars())
    has_more = len(rows) > limit
    return list(reversed(rows[:limit])), has_more


def get(session: Session, friend_id: str, message_id: str) -> Message:
    """取某条消息（**必须属于这个朋友**，别让人拿别的 id 乱翻）。"""
    message = session.get(Message, message_id)
    if message is None or message.friend_id != friend_id:
        raise NotFoundError("MESSAGE_NOT_FOUND", "找不到这条消息")
    return message


def _json_list(raw: str | None) -> list[str]:
    """JSON 数组字段的安全解析（坏了就当空）。"""
    if not raw:
        return []
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return []
    return [str(item) for item in parsed] if isinstance(parsed, list) else []


def to_dict(message: Message) -> dict:
    # v2（D85）：音频**附在文字回复上**。这里只回状态/地址/时长/失败原因，
    # 不回文件路径（前端不该知道磁盘结构）。
    from backend.services.voice_reply_service import audio_url, has_audio, segments_public

    image_url = None
    if message.image_path:
        stem = message.image_path.rsplit("/", 1)[-1].rsplit(".", 1)[0]
        image_url = f"/api/images/{stem}"

    sources = None
    if message.sources:
        try:
            parsed = json.loads(message.sources)
            sources = parsed if isinstance(parsed, list) else None
        except json.JSONDecodeError:
            sources = None

    return {
        "id": message.id,
        "role": message.role,
        "content": message.content,
        "mode_at_send": message.mode_at_send,
        "is_proactive": bool(message.is_proactive),
        "stream_status": message.stream_status,
        "tools_used": message.tools_used,
        "audio_status": message.audio_status,
        "audio_url": audio_url(message) if has_audio(message) else None,
        "audio_duration_ms": message.audio_duration_ms,
        "audio_error": message.audio_error,
        # v2.2（D105）：分段语音（沉浸模式"一段一段说"）；没有就是空数组
        "audio_segments": segments_public(message),
        "image_url": image_url,
        "image_no": message.image_no,
        "sources": sources,
        # v2.2（D102）：表情（沉浸模式 / 桌宠用）
        "mood": message.mood,
        # v2.2（D113）：**每段**的情绪（按段顺序）
        "moods": _json_list(message.moods),
        # v6.0（D298）：一条回复内部的多个气泡；空数组 = 普通单气泡
        "segments": _json_list(message.segments),
        "created_at": message.created_at,
    }
