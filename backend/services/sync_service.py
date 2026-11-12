"""三设备手动同步：逻辑包导出 / 合并 / 墓碑 / 附件（D302）。

不用 SQLite 文件同步：WAL、锁、schema 版本都会出问题。这里只传逻辑 JSON。
"""

from __future__ import annotations

import hashlib
import json
import mimetypes
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

from sqlalchemy import and_, delete, or_, select
from sqlalchemy.orm import Session

from backend import config
from backend.models import (
    Document,
    DocumentChunk,
    Friend,
    FriendEpisode,
    FriendExpression,
    FriendLife,
    FriendState,
    FriendStyleEvent,
    FriendStyleProfile,
    LifeActivity,
    LifeProject,
    Memory,
    MemoryEmbedding,
    MemoryEvent,
    Message,
    Schedule,
    ScheduleEvent,
    Settings,
    Thought,
    SyncBlob,
    SyncConflict,
    SyncDevice,
    SyncMessageOutbox,
    SyncMessageRelay,
    SyncMeta,
    SyncTombstone,
)
from backend.services import settings_service
from backend.services.actor import Actor

APP_ID = "yumeFriend"
SYNC_SCHEMA = 1

# 同步范围：这些是“她的数据”。游戏 / 日志 / 缓存 / 语音文件不在里面。
APPEND_ONLY_MODELS = (
    ("memory_events", MemoryEvent),
    ("schedule_events", ScheduleEvent),
    ("friend_style_events", FriendStyleEvent),
)
UPSERT_MODELS = (
    ("friends", Friend),
    ("messages", Message),
    ("memories", Memory),
    ("friend_states", FriendState),
    ("friend_episodes", FriendEpisode),
    ("friend_expressions", FriendExpression),
    ("friend_style_profiles", FriendStyleProfile),
    ("schedules", Schedule),
    ("thoughts", Thought),
    ("documents", Document),
    ("life_projects", LifeProject),
    ("life_activities", LifeActivity),
    ("friend_life", FriendLife),
)

# 附件目录：语音 / 日志 / 缓存 / 临时上传不进同步包。
MEDIA_DIRS = (
    "avatars",
    "portraits",
    "portrait_packs",
    "backgrounds",
    "images",
    "documents",
    "live2d",
)
_sha_cache: dict[tuple[str, int, int], str] = {}

SAFE_SETTINGS_FIELDS = (
    "active_message_frequency",
    "deep_thinking",
    "show_tool_calls",
    "memory_token_budget",
    "voice_keep_days",
    "my_name",
    "autonomy_owner_device_id",
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def mark_message_dirty(session: Session, message_id: str) -> None:
    """把一条本机新建 / 改过的消息放入待推送队列（D382）。"""
    if not message_id:
        return
    row = session.get(SyncMessageOutbox, message_id)
    changed_at = _now()
    if row is None:
        session.add(SyncMessageOutbox(message_id=message_id, changed_at=changed_at))
    else:
        row.changed_at = changed_at


def ack_messages(session: Session, message_ids: Iterable[str]) -> int:
    """服务器确认过这些消息后，清掉对应的待推送项。"""
    ids = sorted({str(item) for item in message_ids if item})
    if not ids:
        return 0
    result = session.execute(
        delete(SyncMessageOutbox).where(SyncMessageOutbox.message_id.in_(ids))
    )
    return int(result.rowcount or 0)


def clear_message_outbox(session: Session) -> None:
    """全量推送 / 拉取覆盖消息后，旧待推送项已没有意义。"""
    session.execute(delete(SyncMessageOutbox))


def mark_messages_relayed(session: Session, message_ids: Iterable[str]) -> int:
    """服务器合并消息后记一条转发水位，供其他设备按游标拉取（D382）。"""
    count = 0
    now = _now()
    for message_id in sorted({str(item) for item in message_ids if item}):
        row = session.get(SyncMessageRelay, message_id)
        if row is None:
            session.add(SyncMessageRelay(message_id=message_id, relayed_at=now))
        else:
            row.relayed_at = now
        count += 1
    return count


def mark_all_messages_relayed(session: Session) -> int:
    """全量覆盖后，把当前所有消息标成一次待转发。

    这样其他设备下一次增量交换就能收到整份快照，而不是因为消息的
    `updated_at` 很旧而被时间游标跳过。
    """
    ids = list(session.execute(select(Message.id)).scalars())
    return mark_messages_relayed(session, ids)


def _uid() -> str:
    return str(uuid.uuid4())


def _row_dict(row) -> dict:
    return {
        key: value
        for key, value in vars(row).items()
        if not key.startswith("_sa_")
    }


def _get_meta(session: Session, key: str, default: str = "") -> str:
    row = session.get(SyncMeta, key)
    return row.value if row is not None else default


def _set_meta(session: Session, key: str, value: str) -> None:
    row = session.get(SyncMeta, key)
    if row is None:
        row = SyncMeta(key=key, value=value, updated_at=_now())
        session.add(row)
    else:
        row.value = value
        row.updated_at = _now()


def device_id(session: Session) -> str:
    existing = _get_meta(session, "device_id")
    if existing:
        return existing
    value = _uid()
    _set_meta(session, "device_id", value)
    session.flush()
    return value


def device_name(session: Session) -> str:
    return _get_meta(session, "device_name") or "yumeFriend 设备"


def device_kind(session: Session) -> str:
    return _get_meta(session, "device_kind") or "unknown"


def configure_device(session: Session, *, name: str, kind: str) -> None:
    device_id(session)
    _set_meta(session, "device_name", name.strip() or "yumeFriend 设备")
    _set_meta(session, "device_kind", kind.strip() or "unknown")
    # D303：默认主设备 = 手机；只在本机还没设置过时自动落一次。
    if kind == "phone":
        row = settings_service.get(session)
        if not row.autonomy_owner_device_id:
            row.autonomy_owner_device_id = device_id(session)
            row.updated_at = _now()
    session.flush()


def remember_device(session: Session, peer: dict) -> None:
    peer_id = str(peer.get("device_id") or "").strip()
    if not peer_id:
        return
    row = session.get(SyncDevice, peer_id)
    now = _now()
    if row is None:
        row = SyncDevice(
            id=peer_id,
            name=str(peer.get("name") or "yumeFriend 设备"),
            kind=str(peer.get("kind") or "unknown"),
            last_seen_at=now,
            created_at=now,
        )
        session.add(row)
    else:
        row.name = str(peer.get("name") or row.name)
        row.kind = str(peer.get("kind") or row.kind)
        row.last_seen_at = now
    session.flush()


def tombstone(session: Session, entity: str, entity_id: str) -> None:
    """记录物理删除。同步时用它防止另一台设备把记录复活。"""
    existing = session.execute(
        select(SyncTombstone).where(
            SyncTombstone.entity == entity,
            SyncTombstone.entity_id == entity_id,
        )
    ).scalars().first()
    if existing is not None:
        existing.deleted_at = _now()
        existing.device_id = device_id(session)
    else:
        session.add(
            SyncTombstone(
                id=_uid(),
                entity=entity,
                entity_id=entity_id,
                deleted_at=_now(),
                device_id=device_id(session),
            )
        )
    session.flush()


def _safe_rel(path: str | None) -> str | None:
    if not path:
        return None
    text = str(path).replace("\\", "/").lstrip("/")
    if ".." in Path(text).parts:
        return None
    return text


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _sha256_file_cached(path: Path) -> str:
    stat = path.stat()
    key = (str(path), stat.st_mtime_ns, stat.st_size)
    cached = _sha_cache.get(key)
    if cached:
        return cached
    digest = _sha256_file(path)
    # 同一路径换了内容后旧 key 自然失效；顺手清掉这路径的旧项。
    for old in [item for item in _sha_cache if item[0] == str(path) and item != key]:
        _sha_cache.pop(old, None)
    _sha_cache[key] = digest
    return digest


def attachment_manifest() -> list[dict]:
    """当前 user_data 下需要同步的附件（不含音频 / 日志 / 缓存）。"""
    result: list[dict] = []
    base = Path(config.USER_DATA_DIR)
    for dirname in MEDIA_DIRS:
        folder = base / dirname
        if not folder.is_dir():
            continue
        for path in folder.rglob("*"):
            if not path.is_file():
                continue
            rel = path.relative_to(base).as_posix()
            result.append(
                {
                    "path": rel,
                    "sha256": _sha256_file_cached(path),
                    "size": path.stat().st_size,
                    "mime": mimetypes.guess_type(path.name)[0] or "application/octet-stream",
                }
            )
    return result


def missing_attachments(items: Iterable[dict]) -> list[dict]:
    """只保留本地没有 / 内容不一致的附件，避免每次全量重传图片。"""
    base = Path(config.USER_DATA_DIR)
    missing: list[dict] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        rel = _safe_rel(item.get("path"))
        sha = str(item.get("sha256") or "")
        if rel is None or not sha:
            continue
        path = base / rel
        if not path.is_file() or _sha256_file_cached(path) != sha:
            missing.append(item)
    return missing


def export_bundle(
    session: Session,
    *,
    since: str | None = None,
    include_attachments: bool = True,
    chat_only: bool = False,
    use_outbox: bool = True,
    use_relay: bool = False,
) -> dict:
    """导出逻辑包。

    `since` 是服务器模式的增量游标：只导出这个时间之后变化的行。
    本地局域网同步仍按老口径传空值，导出全量。
    """
    tables: dict[str, list[dict]] = {}
    chat_tables = {"messages", "memories", "memory_events", "friend_states"}
    for name, model in APPEND_ONLY_MODELS + UPSERT_MODELS:
        if chat_only and name not in chat_tables:
            continue
        stmt = select(model)
        if name == "memories":
            # 软删的记忆不再当“活数据”同步；删除靠 sync_tombstones 传播。
            stmt = stmt.where(Memory.deleted_at.is_(None))
        # 说话档案只有一行、体积很小；即使游标已经越过旧 updated_at，也要每次带上，
        # 否则旧服务器漏收过这张表后，客户端永远补不回来（D363）。
        if (
            since
            and name != "messages"
            and name not in ("friend_style_profiles", "life_projects")
        ):
            timestamp_column = None
            # 消息流式时先占位、结束后回填正文；必须优先用 updated_at 判断“后来改过”。
            for candidate in ("updated_at", "created_at"):
                if hasattr(model, candidate):
                    timestamp_column = getattr(model, candidate)
                    break
            if timestamp_column is not None:
                stmt = stmt.where(timestamp_column > since)
        if name == "messages":
            if since:
                # D382：时间游标之外，还要带上“本机待推送”的消息（防止旧消息被
                # 游标跳过），以及服务器明确标记过“刚收到、需要转发”的消息。
                conditions = [
                    Message.updated_at.is_(None),
                    Message.updated_at > since,
                ]
                if use_outbox:
                    conditions.append(
                        Message.id.in_(select(SyncMessageOutbox.message_id))
                    )
                if use_relay:
                    conditions.append(
                        Message.id.in_(
                            select(SyncMessageRelay.message_id).where(
                                SyncMessageRelay.relayed_at > since
                            )
                        )
                    )
                stmt = stmt.where(or_(*conditions))
            # 不把“还在流式生成”或空助手占位同步出去，否则别的设备会永久看到空回复。
            stmt = stmt.where(
                or_(
                    Message.role != "assistant",
                    and_(
                        Message.content != "",
                        or_(
                            Message.stream_status.is_(None),
                            Message.stream_status != "streaming",
                        ),
                    ),
                )
            )
        rows = session.execute(stmt).scalars().all()
        tables[name] = [_row_dict(row) for row in rows]

    if not chat_only:
        chunk_stmt = select(DocumentChunk)
        if since:
            chunk_stmt = chunk_stmt.where(DocumentChunk.created_at > since)
        chunks = session.execute(chunk_stmt).scalars().all()
        tables["document_chunks"] = [_row_dict(row) for row in chunks]

        settings_row = settings_service.get(session)
        # D319：LLM 端点 / model / Key 都是设备本地配置，不跨设备同步。
        # 否则 A 机 base_url + B 机旧 Key 的错配会让聊天/反思静默拿到空回复。
        tables["settings"] = [
            {
                "updated_at": settings_row.updated_at,
                **{field: getattr(settings_row, field) for field in SAFE_SETTINGS_FIELDS},
            }
        ]

    tombstones = []
    if chat_only:
        # 聊天/记忆同步范围带记忆与记忆事件墓碑；消息不做墓碑（D368）。
        tombstone_stmt = select(SyncTombstone).where(
            SyncTombstone.entity.in_(("memories", "memory_events", "messages"))
        )
        if since:
            tombstone_stmt = tombstone_stmt.where(SyncTombstone.deleted_at > since)
        tombstones = session.execute(tombstone_stmt).scalars().all()
    elif not chat_only:
        tombstone_stmt = select(SyncTombstone)
        if since:
            tombstone_stmt = tombstone_stmt.where(SyncTombstone.deleted_at > since)
        tombstones = session.execute(tombstone_stmt).scalars().all()
    return {
        "app": APP_ID,
        "schema": SYNC_SCHEMA,
        "device_id": device_id(session),
        "device_name": device_name(session),
        "device_kind": device_kind(session),
        "generated_at": _now(),
        "tables": tables,
        "tombstones": [_row_dict(row) for row in tombstones],
        "attachments": attachment_manifest() if include_attachments else [],
    }


def export_chat_bundle(session: Session) -> dict:
    """导出全量聊天/记忆快照，不夹带朋友资料、生活线、知识库等其它表。"""
    return export_bundle(
        session,
        since=None,
        include_attachments=False,
        chat_only=True,
    )


def export_memory_bundle(session: Session) -> dict:
    """只导出记忆范围（memories + memory_events + 记忆墓碑）。"""
    bundle = export_chat_bundle(session)
    tables = bundle.get("tables") or {}
    bundle["tables"] = {
        name: rows
        for name, rows in tables.items()
        if name in ("memories", "memory_events")
    }
    return bundle


def _newer(local, remote, field: str = "updated_at") -> bool:
    local_value = getattr(local, field, None) or ""
    remote_value = (remote or {}).get(field) or ""
    return remote_value > local_value


def _record_conflict(session: Session, entity: str, entity_id: str, local, remote) -> None:
    session.add(
        SyncConflict(
            id=_uid(),
            entity=entity,
            entity_id=entity_id,
            local_updated_at=str(getattr(local, "updated_at", "") or ""),
            remote_updated_at=str((remote or {}).get("updated_at") or ""),
            detail=json.dumps(
                {"local": _row_dict(local), "remote": remote},
                ensure_ascii=False,
            )[:20000],
            created_at=_now(),
        )
    )


def _upsert_rows(
    session: Session,
    name: str,
    model,
    rows: Iterable[dict],
    *,
    changed_ids: list[str] | None = None,
) -> int:
    imported = 0
    local_voice_ids: set[str] = set()
    if name == "friends":
        from backend.models import Voice  # noqa: PLC0415

        local_voice_ids = {
            str(row.id) for row in session.execute(select(Voice)).scalars().all()
        }
    for raw in rows:
        if not isinstance(raw, dict):
            continue
        primary_keys = [column.name for column in model.__table__.primary_key.columns]
        raw_pk = raw.get("id") or raw.get("key")
        if not raw_pk and primary_keys:
            raw_pk = raw.get(primary_keys[0])
        pk = str(raw_pk or "")
        if not pk:
            continue
        tombstoned = session.execute(
            select(SyncTombstone).where(
                SyncTombstone.entity == name,
                SyncTombstone.entity_id == pk,
            )
        ).scalars().first()
        if tombstoned is not None:
            # 删除优先：已经打墓碑的记录，任何旧设备都不能把它复活。
            continue
        local = session.get(model, pk)
        if local is None:
            data = {key: value for key, value in raw.items() if hasattr(model, key)}
            if name == "friends" and data.get("voice_id") not in local_voice_ids:
                data["voice_id"] = None
            session.add(model(**data))
            imported += 1
            if changed_ids is not None:
                changed_ids.append(pk)
            continue
        allow_update = _newer(local, raw)
        if name == "messages":
            local_content = str(getattr(local, "content", "") or "").strip()
            remote_content = str(raw.get("content") or "").strip()
            remote_status = str(raw.get("stream_status") or "")
            # 空占位永远不能盖掉已经有内容的正文；反过来，完整正文可以修复旧空占位。
            if local_content and (not remote_content or remote_status == "streaming"):
                continue
            if not local_content and remote_content:
                allow_update = True
        if allow_update:
            if getattr(local, "updated_at", None) and raw.get("updated_at"):
                _record_conflict(session, name, pk, local, raw)
            for key, value in raw.items():
                if key != "id" and hasattr(local, key):
                    if name == "friends" and key == "voice_id" and value not in local_voice_ids:
                        value = None
                    setattr(local, key, value)
            imported += 1
            if changed_ids is not None:
                changed_ids.append(pk)
    return imported


def _append_rows(session: Session, model, rows: Iterable[dict]) -> int:
    imported = 0
    entity = str(getattr(model, "__tablename__", "") or "")
    for raw in rows:
        if not isinstance(raw, dict):
            continue
        pk = str(raw.get("id") or "")
        if not pk or session.get(model, pk) is not None:
            continue
        if entity:
            tombstoned = session.execute(
                select(SyncTombstone).where(
                    SyncTombstone.entity == entity,
                    SyncTombstone.entity_id == pk,
                )
            ).scalars().first()
            if tombstoned is not None:
                continue
        data = {key: value for key, value in raw.items() if hasattr(model, key)}
        session.add(model(**data))
        imported += 1
    return imported


def _apply_tombstones(session: Session, rows: Iterable[dict]) -> int:
    applied = 0
    entity_models = {
        "friends": Friend,
        "messages": Message,
        "memories": Memory,
        "memory_events": MemoryEvent,
        "friend_episodes": FriendEpisode,
        "schedules": Schedule,
        "thoughts": Thought,
        "documents": Document,
    }
    for raw in rows:
        if not isinstance(raw, dict):
            continue
        entity = str(raw.get("entity") or "")
        entity_id = str(raw.get("entity_id") or "")
        if not entity or not entity_id:
            continue
        existing = session.execute(
            select(SyncTombstone).where(
                SyncTombstone.entity == entity,
                SyncTombstone.entity_id == entity_id,
            )
        ).scalars().first()
        if existing is None:
            session.add(
                SyncTombstone(
                    id=str(raw.get("id") or _uid()),
                    entity=entity,
                    entity_id=entity_id,
                    deleted_at=str(raw.get("deleted_at") or _now()),
                    device_id=str(raw.get("device_id") or ""),
                )
            )
        model = entity_models.get(entity)
        if model is not None:
            local = session.get(model, entity_id)
            if local is not None:
                # 文件的物理清理跟随记录删除，别留 orphan。
                if entity == "friends":
                    from backend.services import friend_service  # noqa: PLC0415

                    try:
                        friend_service.purge(session, Actor.USER, entity_id)
                        applied += 1
                        continue
                    except Exception:  # noqa: BLE001 —— 记录已经不在时直接走兜底
                        pass
                if entity == "documents":
                    from backend.services import knowledge_service  # noqa: PLC0415

                    try:
                        knowledge_service.delete(session, str(local.friend_id), entity_id)
                        applied += 1
                        continue
                    except Exception:  # noqa: BLE001
                        pass
                session.delete(local)
                applied += 1
    return applied


def _merge_settings(session: Session, rows: list[dict]) -> int:
    if not rows:
        return 0
    remote = rows[0]
    row = settings_service.get(session)
    if not str(remote.get("updated_at") or "") > str(row.updated_at or ""):
        return 0
    for field in SAFE_SETTINGS_FIELDS:
        if field in remote and hasattr(row, field):
            setattr(row, field, remote[field])
    # D319：旧包可能还带 llm_bases / purposes；一律忽略，模型配置留在本机。
    row.updated_at = str(remote.get("updated_at") or _now())
    session.flush()
    return 1


def _merge_document_chunks(session: Session, rows: Iterable[dict]) -> int:
    imported = 0
    for raw in rows:
        if not isinstance(raw, dict):
            continue
        pk = str(raw.get("id") or "")
        if not pk or session.get(DocumentChunk, pk) is not None:
            continue
        data = {key: value for key, value in raw.items() if hasattr(DocumentChunk, key)}
        session.add(DocumentChunk(**data))
        imported += 1
    return imported


def merge_bundle(
    session: Session,
    bundle: dict,
    *,
    relay_incoming_messages: bool = False,
) -> dict:
    if bundle.get("app") != APP_ID or int(bundle.get("schema") or 0) != SYNC_SCHEMA:
        raise ValueError("不是可识别的 yumeFriend 同步包")
    peer = {
        "device_id": bundle.get("device_id"),
        "name": bundle.get("device_name"),
        "kind": bundle.get("device_kind"),
    }
    remember_device(session, peer)
    tables = bundle.get("tables") or {}
    counts: dict[str, int] = {}
    changed_message_ids: list[str] = []
    # 先 upsert 父表（friends / documents），再插消息和 chunks，避免 FK autoflush 失败。
    for name, model in UPSERT_MODELS:
        counts[name] = _upsert_rows(
            session,
            name,
            model,
            tables.get(name) or [],
            changed_ids=changed_message_ids if name == "messages" else None,
        )
    for name, model in APPEND_ONLY_MODELS:
        counts[name] = _append_rows(session, model, tables.get(name) or [])
    counts["document_chunks"] = _merge_document_chunks(session, tables.get("document_chunks") or [])
    counts["settings"] = _merge_settings(session, tables.get("settings") or [])
    counts["tombstones"] = _apply_tombstones(session, bundle.get("tombstones") or [])
    if relay_incoming_messages and changed_message_ids:
        # 同一个包里可能同时带“新消息 + 删除墓碑”；只给仍然存在的消息记转发水位。
        existing_ids = set(
            session.execute(
                select(Message.id).where(Message.id.in_(changed_message_ids))
            ).scalars()
        )
        counts["relayed_messages"] = mark_messages_relayed(session, existing_ids)
    # D303：手机在没有明确主设备时自动成为主设备。
    if device_kind(session) == "phone":
        row = settings_service.get(session)
        if not row.autonomy_owner_device_id:
            row.autonomy_owner_device_id = device_id(session)
            row.updated_at = _now()
    session.flush()
    missing = missing_attachments(bundle.get("attachments") or [])
    return {"ok": True, "imported": counts, "attachments": missing}


def replace_chat_bundle(session: Session, bundle: dict) -> dict:
    """全量覆盖聊天/记忆范围；不动朋友、游戏、生活线、知识库。"""
    if bundle.get("app") != APP_ID or int(bundle.get("schema") or 0) != SYNC_SCHEMA:
        raise ValueError("不是可识别的 yumeFriend 同步包")
    remember_device(
        session,
        {
            "device_id": bundle.get("device_id"),
            "name": bundle.get("device_name"),
            "kind": bundle.get("device_kind"),
        },
    )
    tables = bundle.get("tables") or {}
    # 先清掉本机旧范围；memory_embeddings 在 memory 之前删。
    session.execute(delete(SyncMessageOutbox))
    session.execute(delete(SyncMessageRelay))
    session.execute(delete(Message))
    session.execute(delete(MemoryEmbedding))
    session.execute(delete(Memory))
    session.execute(delete(MemoryEvent))
    session.execute(delete(FriendState))
    session.flush()

    counts: dict[str, int] = {}
    wanted = {"messages", "memories", "friend_states"}
    for name, model in UPSERT_MODELS:
        if name in wanted:
            counts[name] = _upsert_rows(session, name, model, tables.get(name) or [])
    counts["memory_events"] = _append_rows(
        session, MemoryEvent, tables.get("memory_events") or []
    )
    counts["tombstones"] = _apply_tombstones(session, bundle.get("tombstones") or [])
    session.flush()
    return {"ok": True, "imported": counts, "attachments": []}


def replace_memory_bundle(session: Session, bundle: dict) -> dict:
    """只全量覆盖记忆范围；不动消息、朋友状态、朋友资料等。"""
    if bundle.get("app") != APP_ID or int(bundle.get("schema") or 0) != SYNC_SCHEMA:
        raise ValueError("不是可识别的 yumeFriend 同步包")
    remember_device(
        session,
        {
            "device_id": bundle.get("device_id"),
            "name": bundle.get("device_name"),
            "kind": bundle.get("device_kind"),
        },
    )
    tables = bundle.get("tables") or {}
    session.execute(delete(MemoryEmbedding))
    session.execute(delete(Memory))
    session.execute(delete(MemoryEvent))
    session.flush()

    counts: dict[str, int] = {}
    for name, model in UPSERT_MODELS:
        if name == "memories":
            counts[name] = _upsert_rows(session, name, model, tables.get(name) or [])
    counts["memory_events"] = _append_rows(
        session, MemoryEvent, tables.get("memory_events") or []
    )
    counts["tombstones"] = _apply_tombstones(session, bundle.get("tombstones") or [])
    session.flush()
    return {"ok": True, "imported": counts, "attachments": []}


def write_attachment(rel_path: str, data: bytes, sha256: str | None = None) -> str:
    safe = _safe_rel(rel_path)
    if safe is None:
        raise ValueError("附件路径不合法")
    target = Path(config.USER_DATA_DIR) / safe
    base = Path(config.USER_DATA_DIR).resolve()
    resolved = target.resolve()
    if base not in resolved.parents and resolved != base:
        raise ValueError("附件路径越界")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(data)
    digest = sha256 or hashlib.sha256(data).hexdigest()
    if hashlib.sha256(data).hexdigest() != digest:
        raise ValueError("附件哈希不一致")
    return safe


def blob_path(rel_path: str) -> Path | None:
    safe = _safe_rel(rel_path)
    if safe is None:
        return None
    path = Path(config.USER_DATA_DIR) / safe
    return path if path.is_file() else None


def record_blob(session: Session, sha256: str, size: int, mime: str, path: str) -> None:
    row = session.get(SyncBlob, sha256)
    if row is None:
        session.add(
            SyncBlob(
                sha256=sha256,
                size=size,
                mime=mime,
                path=path,
                created_at=_now(),
            )
        )
    else:
        row.size = size
        row.mime = mime
        row.path = path
    session.flush()
