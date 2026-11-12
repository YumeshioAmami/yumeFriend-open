"""记忆业务逻辑（api.md §3 / data_model.md §4 / 宪法附录 B）。

权限模型是这份文件的核心：
  · 用户：对所有记忆有完整 CRUD
  · LLM（Actor.AGENT）：**对所有 layer 都能 增 / 查 / 改（含改层、升降级）/ 删（软删）**（D230）
    · 例外：`rp_play_style`（演法）由专用工具 `adjust_rp_playstyle` 管，不走通用 CRUD
    · `tag` 不可改（`notebook` 是"用户亲手写的"标记）
  · 例外：写入去重是**系统维护行为**（D28 / D29）
  · 每次改 / 删 / 建都写一条 `memory_events` 留痕（D230）
"""

import json
import re
import uuid
from datetime import datetime, timedelta, timezone
from difflib import SequenceMatcher

from loguru import logger
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from backend.models import Memory, MemoryEvent
from backend.services.actor import Actor
from backend.services.errors import NotFoundError, PermissionError_

VALID_TAGS = ("normal", "rp", "rp_play_style", "notebook")
VALID_LAYERS = ("pinned", "permanent", "long_term", "short_term")

# 写入去重（D28）：同一个 friend + 同一个 layer 的最近 N 条里找
DEDUP_WINDOW = 30
# D29 要求"阈值取高、宁可留重复也不误合并"。
# 0.85 是**实测定标**出来的（见 D48）：该合的样本最低 0.857，不该合的最高 0.769。
DEDUP_THRESHOLD = 0.85

SHORT_TERM_DAYS = 7
RECALL_LIMIT = 10


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _new_id() -> str:
    return str(uuid.uuid4())


def _record_event(
    session: Session,
    *,
    friend_id: str,
    memory_id: str | None,
    actor: Actor | str,
    action: str,
    before: dict | None = None,
    after: dict | None = None,
) -> None:
    """留痕（D230）：谁动了哪条记忆、改了哪些字段（旧值 → 新值）。

    `actor` 可以是 `Actor` 或字符串（写入去重是系统行为 → `"system"`，见 D29）。
    **best-effort**：留痕失败只记 WARNING，绝不拖垮主操作。
    """
    name = actor.value if isinstance(actor, Actor) else str(actor)
    try:
        session.add(
            MemoryEvent(
                id=_new_id(),
                friend_id=friend_id,
                memory_id=memory_id,
                actor=name,
                action=action,
                before=json.dumps(before, ensure_ascii=False) if before else None,
                after=json.dumps(after, ensure_ascii=False) if after else None,
                created_at=_now(),
            )
        )
        session.flush()
    except Exception as exc:  # noqa: BLE001 —— 留痕失败不该影响这次对话
        logger.warning("写 memory_events 失败（不影响这次操作）：{}", exc)


def _normalized(text: str) -> str:
    """去掉空白与常见标点再比，避免"5月20号 / 5 月 20 号"这种差异拉低相似度。"""
    return re.sub(r"[\s，。、！？!?,.;:：；「」（）()\[\]【】\"'~～\-—_]+", "", text).lower()


def similarity(left: str, right: str) -> float:
    """纯字符串相似度（不调 LLM，D28）。"""
    a, b = _normalized(left), _normalized(right)
    if not a or not b:
        return 0.0
    return SequenceMatcher(None, a, b).ratio()


def _require_can_modify(memory: Memory, actor: Actor) -> None:
    """谁能改 / 删这条记忆。

    v3.45（D230）：**朋友对所有 layer 都能改 / 删**（原来只限 pinned）。
    唯一例外：`rp_play_style`（演法）不走通用 CRUD —— 它由专用工具
    `adjust_rp_playstyle` 维护，免得被当成普通记忆改坏。用户不受限。
    """
    if actor is Actor.USER:
        return
    if memory.tag == "rp_play_style":
        raise PermissionError_(
            "rp_play_style（演法）请用 adjust_rp_playstyle 改，不要走通用记忆 CRUD（D230）"
        )


def _require(session: Session, memory_id: str) -> Memory:
    memory = session.get(Memory, memory_id)
    if memory is None or memory.deleted_at is not None:
        raise NotFoundError("MEMORY_NOT_FOUND", "找不到该记忆")
    return memory


def list_alive(session: Session, friend_id: str) -> list[Memory]:
    """该 friend 的所有未删除记忆（**朋友之间严格隔离**：WHERE friend_id，宪法附录 B）。"""
    stmt = select(Memory).where(
        Memory.friend_id == friend_id,
        Memory.deleted_at.is_(None),
    )
    return list(session.execute(stmt).scalars())


# 列表排序：默认「最近提起」，另外支持「重要度」「写入时间」
SORT_OPTIONS = ("recent", "importance", "created")


def search(
    session: Session,
    friend_id: str,
    *,
    layer: str | None = None,
    tag: str | None = None,
    text: str | None = None,
    limit: int = 50,
    offset: int = 0,
    sort: str = "recent",
) -> tuple[list[Memory], int]:
    """记忆浏览 / 列表（api.md §3.1）。返回 `(本页, 总数)`。"""
    conditions = [Memory.friend_id == friend_id, Memory.deleted_at.is_(None)]
    if layer:
        conditions.append(Memory.layer == layer)
    if tag:
        conditions.append(Memory.tag == tag)
    if text:
        conditions.append(Memory.content.like(f"%{text}%"))

    total = len(list(session.execute(select(Memory).where(*conditions)).scalars()))
    order = {
        "recent": Memory.refreshed_at.desc(),
        "importance": Memory.importance.desc(),
        "created": Memory.created_at.desc(),
    }.get(sort, Memory.refreshed_at.desc())
    stmt = select(Memory).where(*conditions).order_by(order).limit(limit).offset(offset)
    return list(session.execute(stmt).scalars()), total


def layer_counts(session: Session, friend_id: str) -> dict[str, int]:
    """各层的条数 —— 界面上做 tab 角标用（几百上千条时靠它一眼看清分布）。"""
    from sqlalchemy import func

    rows = session.execute(
        select(Memory.layer, func.count())
        .where(Memory.friend_id == friend_id, Memory.deleted_at.is_(None))
        .group_by(Memory.layer)
    ).all()
    counts = {layer: 0 for layer in VALID_LAYERS}
    for layer, count in rows:
        counts[layer] = int(count)
    return counts


def save(
    session: Session,
    actor: Actor,
    friend_id: str,
    *,
    content: str,
    tag: str = "normal",
    layer: str = "pinned",
    context: str | None = None,
    importance: float = 0.5,
) -> tuple[Memory, bool]:
    """新增一条记忆，**先做写入去重**（D28）。

    返回 `(memory, merged)`：`merged=True` 表示命中了已有记忆、把它更新了而不是新增。
    """
    if tag not in VALID_TAGS:
        raise ValueError(f"非法 tag：{tag}")
    if layer not in VALID_LAYERS:
        raise ValueError(f"非法 layer：{layer}")
    content = content.strip()
    if not content:
        raise ValueError("记忆内容不能为空")

    best: Memory | None = None
    best_score = 0.0
    same_layer = [
        m
        for m in list_alive(session, friend_id)
        if m.layer == layer and m.tag != "rp_play_style"
    ]
    same_layer.sort(key=lambda m: m.created_at, reverse=True)
    for candidate in same_layer[:DEDUP_WINDOW]:
        score = similarity(candidate.content, content)
        if score > best_score:
            best, best_score = candidate, score

    now = _now()
    if best is not None and best_score >= DEDUP_THRESHOLD:
        # 系统维护行为（D29）：不是 LLM 在"修改"，所以不受 pinned 限制
        before = {"content": best.content, "importance": float(best.importance)}
        logger.info(
            "记忆去重命中（相似度 {:.2f}），合并进 {}：[{}] → [{}]",
            best_score,
            best.id,
            best.content,
            content,
        )
        best.content = content  # 最新说法为准
        best.importance = max(float(best.importance), float(importance))  # 只升不降
        best.refreshed_at = now  # 又被提起 = 重新鲜活（D28）
        best.updated_at = now
        if context:
            best.context = context
        session.flush()
        # actor 记 "system"：去重合并是系统行为，不冒充"朋友改的"（D29 / D230）
        _record_event(
            session,
            friend_id=friend_id,
            memory_id=best.id,
            actor="system",
            action="update",
            before=before,
            after={
                "content": best.content,
                "importance": float(best.importance),
                "reason": "dedup_merge",
            },
        )
        return best, True

    expires_at = None
    if layer == "short_term":
        expires_at = (datetime.now(timezone.utc) + timedelta(days=SHORT_TERM_DAYS)).isoformat()

    memory = Memory(
        id=_new_id(),
        friend_id=friend_id,
        content=content,
        tag=tag,
        layer=layer,
        context=context,
        importance=float(importance),
        expires_at=expires_at,
        created_at=now,
        refreshed_at=now,
    )
    session.add(memory)
    session.flush()
    _record_event(
        session,
        friend_id=friend_id,
        memory_id=memory.id,
        actor=actor,
        action="create",
        after={
            "content": memory.content,
            "layer": memory.layer,
            "tag": memory.tag,
            "importance": float(memory.importance),
        },
    )
    return memory, False


def update(
    session: Session,
    actor: Actor,
    memory_id: str,
    *,
    content: str | None = None,
    importance: float | None = None,
    layer: str | None = None,
) -> Memory:
    """改内容 / importance / **layer（升 / 降级）**。

    v3.45（D230）：layer 现在**可以改**（原来是"写入即定"）。**`tag` 仍不可改**
    （`notebook` 是"用户亲手写的"标记）。改成 / 改离 `short_term` 时同步维护 `expires_at`。
    """
    memory = _require(session, memory_id)
    _require_can_modify(memory, actor)
    before: dict[str, object] = {}
    after: dict[str, object] = {}
    if content is not None:
        text = content.strip()
        if not text:
            raise ValueError("记忆内容不能为空")
        if text != memory.content:
            before["content"] = memory.content
            after["content"] = text
            memory.content = text
    if importance is not None:
        value = float(importance)
        if value != float(memory.importance):
            before["importance"] = float(memory.importance)
            after["importance"] = value
            memory.importance = value
    if layer is not None:
        if layer not in VALID_LAYERS:
            raise ValueError(f"非法 layer：{layer}")
        if layer != memory.layer:
            before["layer"] = memory.layer
            after["layer"] = layer
            memory.layer = layer
            # 层和"寿命"必须同步：改去 short_term 要有 expires_at；
            # 从 short_term 改走则**必须清掉**，不然它会被 purge_expired 当成到期的短期记忆硬删。
            if layer == "short_term":
                if not memory.expires_at:
                    memory.expires_at = (
                        datetime.now(timezone.utc) + timedelta(days=SHORT_TERM_DAYS)
                    ).isoformat()
            else:
                memory.expires_at = None
    if not before:
        return memory  # 什么都没变，不留痕
    memory.updated_at = _now()
    session.flush()
    _record_event(
        session,
        friend_id=memory.friend_id,
        memory_id=memory.id,
        actor=actor,
        action="update",
        before=before,
        after=after,
    )
    return memory


def delete(session: Session, actor: Actor, memory_id: str) -> None:
    """软删（D230 起：用户与朋友对所有 layer 都能删；仍是软删，30 天可恢复）。"""
    memory = _require(session, memory_id)
    _require_can_modify(memory, actor)
    now = _now()
    memory.deleted_at = now
    memory.updated_at = now
    session.flush()
    _record_event(
        session,
        friend_id=memory.friend_id,
        memory_id=memory.id,
        actor=actor,
        action="delete",
        before={"content": memory.content, "layer": memory.layer, "deleted": False},
        after={"deleted": True},
    )
    # D379：删除必须同步出去，否则另一台设备会把旧记忆再推回来。
    # 同步层把墓碑当作“这条记忆已经不存在”；本机仍保留软删行，30 天内可恢复。
    try:
        from backend.services import sync_service  # noqa: PLC0415

        sync_service.tombstone(session, "memories", memory.id)
    except Exception as exc:  # noqa: BLE001 —— 墓碑失败不能挡住删除本身
        logger.warning("写记忆删除墓碑失败（删除本身已完成）：{}", exc)


def save_play_style(
    session: Session, actor: Actor, friend_id: str, *, role: str, text: str
) -> tuple[Memory, bool]:
    """写"演法"（`tag=rp_play_style`）：**同一个角色只留一条，覆盖更新**（M4）。

    宪法附录 B 的例子就是 `(tag=rp_play_style, layer=long_term)`；
    DB 上有个唯一索引 `(friend_id, context) WHERE tag='rp_play_style'`，
    所以这里**必须**走 upsert —— 直接 INSERT 第二条会撞唯一索引。

    返回 `(memory, updated)`：`updated=True` 表示覆盖了原来那条演法。
    """
    role = role.strip()
    text = text.strip()
    if not role:
        raise ValueError("演法要挂在某个角色上，角色名不能为空")
    if not text:
        raise ValueError("演法内容不能为空")

    existing = session.execute(
        select(Memory).where(
            Memory.friend_id == friend_id,
            Memory.tag == "rp_play_style",
            Memory.context == role,
            Memory.deleted_at.is_(None),
        )
    ).scalar_one_or_none()

    now = _now()
    if existing is not None:
        before = {"content": existing.content}
        logger.info("演法更新（{}）：[{}] → [{}]", role, existing.content, text)
        existing.content = text
        existing.refreshed_at = now
        existing.updated_at = now
        session.flush()
        _record_event(
            session,
            friend_id=friend_id,
            memory_id=existing.id,
            actor=actor,
            action="update",
            before=before,
            after={"content": text, "reason": "play_style"},
        )
        return existing, True

    memory = Memory(
        id=_new_id(),
        friend_id=friend_id,
        content=text,
        tag="rp_play_style",
        layer="long_term",
        context=role,
        importance=0.8,  # 演法比一般记忆更"要紧"：它直接决定怎么演
        created_at=now,
        refreshed_at=now,
    )
    session.add(memory)
    session.flush()
    _record_event(
        session,
        friend_id=friend_id,
        memory_id=memory.id,
        actor=actor,
        action="create",
        after={"content": memory.content, "tag": memory.tag, "reason": "play_style"},
    )
    return memory, False


def recall(
    session: Session,
    friend_id: str,
    query: str,
    *,
    limit: int = RECALL_LIMIT,
) -> list[Memory]:
    """`recall_memory` 工具的实现：v1 用关键词匹配（D27，不做向量检索）。

    检索范围 = 该 friend 的所有现存记忆（严格按 friend 隔离）。
    """
    keywords = [w for w in re.split(r"[\s，。、！？!?,.;:：；]+", query or "") if w]
    if not keywords:
        return []
    # 用整句 OR 关键词命中；命中的放在前面，其余按时间补足
    conditions = [Memory.content.like(f"%{w}%") for w in keywords]
    strict = list(
        session.execute(
            select(Memory).where(
                Memory.friend_id == friend_id,
                Memory.deleted_at.is_(None),
                or_(*conditions),
            )
        ).scalars()
    )
    if len(strict) >= limit:
        return strict[:limit]

    seen = {m.id for m in strict}
    rest = [
        m
        for m in list_alive(session, friend_id)
        if m.id not in seen and m.layer in ("pinned", "permanent", "long_term")
    ]
    rest.sort(key=lambda m: m.refreshed_at, reverse=True)
    return (strict + rest)[:limit]


def to_dict(memory: Memory) -> dict:
    return {
        "id": memory.id,
        "friend_id": memory.friend_id,
        "content": memory.content,
        "tag": memory.tag,
        "layer": memory.layer,
        "context": memory.context,
        "importance": memory.importance,
        "refreshed_at": memory.refreshed_at,
        "expires_at": memory.expires_at,
        "created_at": memory.created_at,
    }


def _loads(raw: str | None) -> dict | None:
    if not raw:
        return None
    try:
        return json.loads(raw)
    except ValueError:
        return None


def list_events(session: Session, friend_id: str, *, limit: int = 50) -> list[MemoryEvent]:
    """最近的记忆改动留痕（D230），新 → 旧。"""
    rows = session.execute(
        select(MemoryEvent)
        .where(MemoryEvent.friend_id == friend_id)
        .order_by(MemoryEvent.created_at.desc())
        .limit(limit)
    ).scalars()
    return list(rows)


def event_to_dict(event: MemoryEvent) -> dict:
    return {
        "id": event.id,
        "memory_id": event.memory_id,
        "actor": event.actor,
        "action": event.action,
        "before": _loads(event.before),
        "after": _loads(event.after),
        "created_at": event.created_at,
    }


def purge_expired(session: Session) -> int:
    """`short_term` 过期硬删（data_model §7 / architecture §8 每天跑一次）。"""
    from backend.services import sync_service  # noqa: PLC0415

    now = _now()
    rows = list(
        session.execute(
            select(Memory).where(
                Memory.expires_at.is_not(None),
                Memory.expires_at < now,
            )
        ).scalars()
    )
    for row in rows:
        sync_service.tombstone(session, "memories", row.id)
        session.delete(row)
    session.flush()
    return len(rows)


def purge_deleted(
    session: Session, *, older_than_days: int = 30, now: str | None = None
) -> int:
    """**软删**超过 N 天的记忆物理删掉（data_model §7.3：用户删的记忆 30 天后清理）。

    跟 `purge_expired`（short_term 到点就硬删）不是一回事：那条是"寿命到了"，
    这条是"用户删了、过了可恢复期"（宪法第九条）。
    """
    from backend.services import sync_service  # noqa: PLC0415

    cutoff = _cutoff_iso(older_than_days, now)
    rows = list(
        session.execute(
            select(Memory).where(
                Memory.deleted_at.is_not(None),
                Memory.deleted_at < cutoff,
            )
        ).scalars()
    )
    for row in rows:
        sync_service.tombstone(session, "memories", row.id)
        session.delete(row)
    session.flush()
    return len(rows)


def _cutoff_iso(older_than_days: int, now: str | None = None) -> str:
    """"N 天前"的 UTC ISO8601 —— 跟表里那些时间戳同一个格式，可以直接比大小。"""
    if now:
        base = datetime.fromisoformat(now)
    else:
        base = datetime.now(timezone.utc)
    return (base - timedelta(days=older_than_days)).isoformat()
