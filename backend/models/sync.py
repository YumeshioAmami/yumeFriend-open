"""三设备同步的元数据 / 删除墓碑 / 冲突留痕（D302）。"""

from __future__ import annotations

from sqlalchemy import ForeignKey, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from backend.db import Base


class SyncMeta(Base):
    """本机同步元数据（device_id / 设备名 / 最近一次同步游标等）。"""

    __tablename__ = "sync_meta"

    key: Mapped[str] = mapped_column(String, primary_key=True)
    value: Mapped[str] = mapped_column(Text, nullable=False, default="")
    updated_at: Mapped[str] = mapped_column(String, nullable=False)


class SyncDevice(Base):
    """发现过 / 同步过的设备。`kind` = phone / tablet / desktop。"""

    __tablename__ = "sync_devices"

    id: Mapped[str] = mapped_column(String, primary_key=True)  # 对端 device_id
    name: Mapped[str] = mapped_column(String, nullable=False)
    kind: Mapped[str] = mapped_column(String, nullable=False, default="unknown")
    last_seen_at: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[str] = mapped_column(String, nullable=False)

    __table_args__ = (Index("idx_sync_devices_seen", "last_seen_at"),)


class SyncTombstone(Base):
    """已经物理删除的记录：同步时必须传播，防止被别的设备复活。"""

    __tablename__ = "sync_tombstones"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    entity: Mapped[str] = mapped_column(String, nullable=False)  # friends/memories/documents...
    entity_id: Mapped[str] = mapped_column(String, nullable=False)
    deleted_at: Mapped[str] = mapped_column(String, nullable=False)
    device_id: Mapped[str] = mapped_column(String, nullable=False)

    __table_args__ = (
        Index("idx_sync_tombstones_entity", "entity", "entity_id"),
        Index("idx_sync_tombstones_time", "deleted_at"),
    )


class SyncConflict(Base):
    """冲突留痕：两端都改过同一条时，保留最新并留下证据。"""

    __tablename__ = "sync_conflicts"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    entity: Mapped[str] = mapped_column(String, nullable=False)
    entity_id: Mapped[str] = mapped_column(String, nullable=False)
    local_updated_at: Mapped[str | None] = mapped_column(String, nullable=True)
    remote_updated_at: Mapped[str | None] = mapped_column(String, nullable=True)
    detail: Mapped[str] = mapped_column(Text, nullable=False, default="")
    created_at: Mapped[str] = mapped_column(String, nullable=False)

    __table_args__ = (Index("idx_sync_conflicts_time", "created_at"),)


class SyncBlob(Base):
    """内容寻址附件缓存（sha256 -> 本地路径 / 大小 / 类型）。"""

    __tablename__ = "sync_blobs"

    sha256: Mapped[str] = mapped_column(String, primary_key=True)
    size: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    mime: Mapped[str] = mapped_column(String, nullable=False, default="application/octet-stream")
    path: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[str] = mapped_column(String, nullable=False)


class SyncMessageOutbox(Base):
    """本机待推送的消息（D382）。

    时间游标只能说明“服务器时间走到哪了”，不能证明本机某条旧消息已经被服务器收过。
    消息一旦创建 / 回填正文，就先记在这里；服务器确认接收后才删。
    """

    __tablename__ = "sync_message_outbox"

    message_id: Mapped[str] = mapped_column(
        String,
        ForeignKey("messages.id", ondelete="CASCADE"),
        primary_key=True,
    )
    changed_at: Mapped[str] = mapped_column(String, nullable=False)


class SyncMessageRelay(Base):
    """服务器侧“已收到并需要转发”的消息水位（D382）。

    客户端可能补传一条创建时间很旧的消息；如果服务器只按 `updated_at > 客户端游标`
    回包，其他设备就永远收不到它。服务器合并成功后在这里写当前时间，专门用于转发。
    """

    __tablename__ = "sync_message_relay"

    message_id: Mapped[str] = mapped_column(
        String,
        ForeignKey("messages.id", ondelete="CASCADE"),
        primary_key=True,
    )
    relayed_at: Mapped[str] = mapped_column(String, nullable=False)
