"""朋友业务逻辑（api.md §1）。

所有写 friends 表的地方都走这里。actor 参数用于 api.md §8 的权限模型：
M1 阶段只有 HTTP 层（Actor.USER），M3 会加 LLM 工具路径（Actor.AGENT）。
"""

import json
import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from backend.models import Friend, Message, Schedule, Voice
from backend.agent.context import clamp_temperature
from backend.agent.parser import strip_mood_marks as parser_strip_mood_marks
from backend.services.actor import Actor
from backend.services import (
    avatar_service,
    background_service,
    knowledge_service,
    portrait_pack_service,
    portrait_service,
)
from backend.services.errors import NotFoundError, ServiceError

VALID_MODES = ("normal", "roleplay", "tool")

# v4.3（D249）：朋友的"温度"三态 —— 用这个哨兵区分"这次不改"和"恢复默认（null）"。
# （普通 None 只能表达"不改"，而"恢复默认"是另一件事，必须能单独表达。）
UNSET: object = object()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _new_id() -> str:
    return str(uuid.uuid4())


def _parse_transform(raw: str | None) -> dict | None:
    """立绘的缩放 / 位移（D110）。坏了就当没有，别让界面炸。"""
    if not raw:
        return None
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if not isinstance(parsed, dict):
        return None
    scale = parsed.get("scale")
    try:
        scale = float(scale) if scale is not None else 1.0
    except (TypeError, ValueError):
        scale = 1.0
    return {
        "scale": min(3.0, max(0.2, scale)),
        "x": float(parsed.get("x") or 0),
        "y": float(parsed.get("y") or 0),
    }


def _voice_examples(raw: str | None) -> list[str]:
    """声音样本：最多 16 段，每段去空白、去空串（D298）。"""
    if not raw:
        return []
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        return []
    if not isinstance(parsed, list):
        return []
    out: list[str] = []
    for item in parsed:
        text = str(item or "").strip()
        if text:
            out.append(text[:800])
        if len(out) >= 16:
            break
    return out


def _voice_examples_json(examples: list[str] | None) -> str:
    return json.dumps(_voice_examples(json.dumps(examples or [], ensure_ascii=False)), ensure_ascii=False)


def _stats(session: Session, friend_id: str) -> dict:
    """列表项需要的三个派生字段（api.md §1.4 / D30）。"""
    last = session.execute(
        select(Message)
        .where(Message.friend_id == friend_id)
        .order_by(Message.created_at.desc())
        .limit(1)
    ).scalar_one_or_none()
    unfinished = session.execute(
        select(func.count())
        .select_from(Message)
        .where(
            Message.friend_id == friend_id,
            Message.stream_status.in_(("streaming", "partial")),
        )
    ).scalar_one()
    return {
        "last_message_preview": (
            parser_strip_mood_marks(last.content)[:60] if last else None
        ),
        "last_message_at": (last.created_at if last else None),
        "has_unfinished": bool(unfinished),
    }


def to_dict(session: Session, friend: Friend, *, detail: bool = False) -> dict:
    data = {
        "id": friend.id,
        "name": friend.name,
        "presence": bool(friend.presence),
        "current_mode": friend.current_mode,
        "current_role": friend.current_role,
        "unread_count": friend.unread_count,
        "life_enabled": bool(getattr(friend, "life_enabled", 0)),
        "chat_pacing_enabled": bool(getattr(friend, "chat_pacing_enabled", 0)),
        "humanizer_enabled": bool(getattr(friend, "humanizer_enabled", 0)),
        # 有头像才给 URL；带 ?v= 是为了换了头像不吃浏览器缓存（v2，T2.0-3）
        "avatar_url": (
            f"/api/friends/{friend.id}/avatar?v={friend.updated_at}"
            if avatar_service.file_path(friend)
            else None
        ),
        # v2.2（D107）：沉浸模式的背景图（没有就是 None）。文件存在就给地址（带 mtime 破缓存）
        "background_url": background_service.url_of(friend.id),
        # v2.2（D109）：她**真有立绘**的情绪名 —— 前端拿它给表情兜底
        # （她没有这一张时别去请求一个 404 的图，退到「正常」，再退大号头像）
        "portrait_emotions": portrait_service.emotions(
            friend.id,
            portrait_pack_service.pack_id_for_friend(
                friend.id,
                getattr(friend, "portrait_pack_id", None),
            ),
        ),
        # v7.43（D441）：呈现方式 / 选中的 Live2D 模型 / 立绘包
        "visual_mode": str(getattr(friend, "visual_mode", "live2d") or "live2d"),
        "live2d_model_id": getattr(friend, "live2d_model_id", None),
        "portrait_pack_id": getattr(friend, "portrait_pack_id", None),
        # v2.2（D110）：立绘的缩放 / 位移（沉浸模式里拖出来的；NULL = 默认位置）
        "portrait_transform": _parse_transform(friend.portrait_transform),
        "created_at": friend.created_at,
        "updated_at": friend.updated_at,
        "deleted_at": friend.deleted_at,
        **_stats(session, friend.id),
    }
    if detail:
        data["personality"] = friend.personality
        data["custom_instructions"] = friend.custom_instructions
        # v2（D84）：「她的样子」。只有详情才带（列表不需要，省点带宽）
        data["avatar_description"] = friend.avatar_description
        # v2（T2.0-4）：用哪个音色（没配就是 null = 不发声）
        data["voice_id"] = friend.voice_id
        # v2（D85）：开了就每条回复都配音
        data["voice_reply_enabled"] = bool(friend.voice_reply_enabled)
        # v3.5（D151）：这个朋友用哪个命名端点（None = 跟随全局「聊天」用途）
        data["llm_base"] = friend.llm_base
        # v4.3（D249）：她的温度（null = 跟随默认）
        data["temperature"] = friend.temperature
        # v6.0（D298）：声音样本（学语气，不是套句式）
        data["voice_examples"] = _voice_examples(getattr(friend, "voice_examples", None))
    return data


def _require(session: Session, friend_id: str, *, allow_deleted: bool = False) -> Friend:
    friend = session.get(Friend, friend_id)
    if friend is None:
        raise NotFoundError("FRIEND_NOT_FOUND", "找不到该朋友")
    if friend.deleted_at is not None and not allow_deleted:
        raise NotFoundError("FRIEND_DELETED", "该朋友已被删除")
    return friend


def create(
    session: Session,
    actor: Actor,
    *,
    name: str,
    personality: str = "",
    custom_instructions: str = "",
    voice_examples: list[str] | None = None,
    temperature: float | None = None,
    life_enabled: bool = False,
    chat_pacing_enabled: bool = False,
    humanizer_enabled: bool = False,
    visual_mode: str = "live2d",
    live2d_model_id: str | None = None,
    portrait_pack_id: str | None = None,
) -> Friend:
    now = _now()
    friend = Friend(
        id=_new_id(),
        name=name,
        personality=personality,
        custom_instructions=custom_instructions,
        voice_examples=_voice_examples_json(voice_examples),
        temperature=clamp_temperature(temperature),
        life_enabled=1 if life_enabled else 0,
        chat_pacing_enabled=1 if chat_pacing_enabled else 0,
        humanizer_enabled=1 if humanizer_enabled else 0,
        visual_mode=visual_mode if visual_mode in ("live2d", "portrait") else "live2d",
        live2d_model_id=live2d_model_id,
        portrait_pack_id=portrait_pack_id,
        created_at=now,
        updated_at=now,
    )
    session.add(friend)
    session.flush()
    # D424：新朋友即使还没导立绘，也先有一张默认「正常」立绘。
    portrait_service.ensure_default(friend.id)
    return friend


def list_friends(session: Session, actor: Actor, *, include_deleted: bool = False) -> list[Friend]:
    stmt = select(Friend).order_by(Friend.created_at)
    if not include_deleted:
        stmt = stmt.where(Friend.deleted_at.is_(None))
    return list(session.execute(stmt).scalars())


def get(session: Session, actor: Actor, friend_id: str) -> Friend:
    return _require(session, friend_id)


def update(
    session: Session,
    actor: Actor,
    friend_id: str,
    *,
    name: str | None = None,
    personality: str | None = None,
    custom_instructions: str | None = None,
    voice_examples: list[str] | None = None,
    avatar_description: str | None = None,
    voice_id: str | None = None,
    voice_reply_enabled: bool | None = None,
    portrait_transform: dict | None = None,
    llm_base: str | None = None,
    temperature: float | None | object = UNSET,
    life_enabled: bool | None = None,
    chat_pacing_enabled: bool | None = None,
    humanizer_enabled: bool | None = None,
    visual_mode: str | None = None,
    live2d_model_id: str | None = None,
    portrait_pack_id: str | None = None,
) -> Friend:
    friend = _require(session, friend_id)
    if name is not None:
        friend.name = name
    if personality is not None:
        friend.personality = personality
    if custom_instructions is not None:
        friend.custom_instructions = custom_instructions
    if voice_examples is not None:
        friend.voice_examples = _voice_examples_json(voice_examples)
    if avatar_description is not None:
        friend.avatar_description = avatar_description
    if voice_id is not None:
        # 空字符串 = 取消配音色；非空必须真存在（不然 flush 时外键会炸成 500）
        if voice_id == "":
            friend.voice_id = None
        else:
            if session.get(Voice, voice_id) is None:
                raise NotFoundError("VOICE_NOT_FOUND", "没有这个音色")
            friend.voice_id = voice_id
    if voice_reply_enabled is not None:
        friend.voice_reply_enabled = 1 if voice_reply_enabled else 0
    if portrait_transform is not None:
        # D110：立绘的缩放 / 位移（沉浸模式里拖出来的）。存 JSON 文本，够小、够稳
        friend.portrait_transform = json.dumps(portrait_transform, ensure_ascii=False)
    if llm_base is not None:
        # v3.5（D151）：空字符串 = 跟随全局「聊天」用途（存 NULL，别存空串）
        friend.llm_base = llm_base.strip() or None
    if temperature is not UNSET:
        # v4.3（D249）：显式给了就设（给 null = 恢复默认）；夹进合理范围
        friend.temperature = clamp_temperature(temperature)  # type: ignore[arg-type]
    if life_enabled is not None:
        friend.life_enabled = 1 if life_enabled else 0
    if chat_pacing_enabled is not None:
        friend.chat_pacing_enabled = 1 if chat_pacing_enabled else 0
    if humanizer_enabled is not None:
        friend.humanizer_enabled = 1 if humanizer_enabled else 0
    if visual_mode is not None:
        if visual_mode not in ("live2d", "portrait"):
            raise ServiceError("FRIEND_BAD_VISUAL_MODE", "呈现方式只能是 Live2D 或立绘包", 400)
        friend.visual_mode = visual_mode
    if live2d_model_id is not None:
        friend.live2d_model_id = live2d_model_id.strip() or None
    if portrait_pack_id is not None:
        friend.portrait_pack_id = portrait_pack_id.strip() or None
    friend.updated_at = _now()
    session.flush()
    return friend


def set_presence(session: Session, actor: Actor, friend_id: str, presence: bool) -> Friend:
    friend = _require(session, friend_id)
    friend.presence = 1 if presence else 0
    friend.updated_at = _now()
    session.flush()
    return friend


def set_mode(
    session: Session, actor: Actor, friend_id: str, mode: str, role: str | None = None
) -> Friend:
    """切 mode / role（api.md §1.2）。

    - 切到非 roleplay → current_role 自动清空
    - mode=roleplay + role=None → 清角色但留在 roleplay
    - 用户强切不需要 LLM 同意（宪法§2）
    """
    if mode not in VALID_MODES:
        raise ValueError(f"非法 mode: {mode}")
    friend = _require(session, friend_id)
    friend.current_mode = mode
    friend.current_role = role if mode == "roleplay" else None
    friend.updated_at = _now()
    session.flush()
    return friend


def soft_delete(session: Session, actor: Actor, friend_id: str) -> None:
    """软删除朋友（data_model §7.1）：连带软删 schedules，防"幽灵日程 / 提醒"。"""
    friend = _require(session, friend_id)
    now = _now()
    friend.deleted_at = now
    friend.updated_at = now
    for schedule in session.execute(
        select(Schedule).where(Schedule.friend_id == friend_id, Schedule.deleted_at.is_(None))
    ).scalars():
        schedule.deleted_at = now
    session.flush()


def restore(session: Session, actor: Actor, friend_id: str) -> Friend:
    friend = _require(session, friend_id, allow_deleted=True)
    friend.deleted_at = None
    friend.updated_at = _now()
    for schedule in session.execute(
        select(Schedule).where(Schedule.friend_id == friend_id, Schedule.deleted_at.is_not(None))
    ).scalars():
        schedule.deleted_at = None
    session.flush()
    return friend


def purge(session: Session, actor: Actor, friend_id: str) -> None:
    """物理删除：messages / memories / schedules 由 FK CASCADE 一并删除（宪法§9）。"""
    friend = _require(session, friend_id, allow_deleted=True)
    from backend.services import sync_service  # noqa: PLC0415 —— 同步墓碑

    sync_service.tombstone(session, "friends", friend_id)
    avatar_service.clear(friend)  # 头像文件也一起清（删除要彻底，宪法第九条）
    from backend import config as backend_config  # noqa: PLC0415

    try:
        (backend_config.AVATAR_MODEL_DIR / f"{friend_id}.vrm").unlink(missing_ok=True)
    except OSError:
        pass
    # v2.1（D98）：知识库的原件也一起清（库里的行由 FK CASCADE 带走）
    from backend.services import knowledge_service  # 懒导入，避免循环

    knowledge_service.clear_files_for_friend(session, friend_id)
    portrait_service.clear_all(friend_id)  # v2.2（D102）：立绘目录也一起清
    background_service.clear(friend_id)  # v2.2（D107）：背景图也一起清
    # v3.0（D141）：**聊天里的图片和语音文件也一起清** —— 以前漏了这一步，
    # 彻底删掉朋友后 images/ 和 audio/ 里会留下永远没人认领的孤儿（宪法§9）。
    # 必须**在删消息行之前**做（行没了就查不出要删哪些文件）。
    from backend.services import image_service, voice_reply_service  # noqa: PLC0415

    image_service.remove_for_friend(session, friend_id)
    voice_reply_service.remove_for_friend(session, friend_id)
    session.delete(friend)
    session.flush()


def list_purgeable(
    session: Session, *, older_than_days: int = 30, now: str | None = None
) -> list[Friend]:
    """**软删**超过 N 天的朋友 —— 每天清理任务物理删它们（data_model §7.1 / 宪法§9）。"""
    cutoff = _cutoff_iso(older_than_days, now)
    stmt = (
        select(Friend)
        .where(Friend.deleted_at.is_not(None), Friend.deleted_at < cutoff)
        .order_by(Friend.deleted_at)
    )
    return list(session.execute(stmt).scalars())


def _cutoff_iso(older_than_days: int, now: str | None = None) -> str:
    """"N 天前"的 UTC ISO8601（跟表里的时间戳同一格式，可以直接比大小）。"""
    base = datetime.fromisoformat(now) if now else datetime.now(timezone.utc)
    return (base - timedelta(days=older_than_days)).isoformat()


def mark_read(session: Session, actor: Actor, friend_id: str) -> None:
    friend = _require(session, friend_id)
    friend.unread_count = 0
    session.flush()
