"""memory_embeddings 表：记忆的向量（D231，本地 ONNX 嵌入，一记忆一行）。

两个刻意设计：
  · `memory_id` 外键 **ON DELETE CASCADE** —— 记忆 30 天后被物理清理时向量一起走
    （宪法§9/§13：删除要彻底、不留 orphan）。
  · `content_hash` 用来判断"内容变了要重算"；回填只补**缺的 / 过期的**，不重复算。

向量存 float32 的原始字节（`array('f')` / `numpy.frombuffer`），不做额外序列化。
"""

from sqlalchemy import ForeignKey, Index, Integer, LargeBinary, String
from sqlalchemy.orm import Mapped, mapped_column

from backend.db import Base


class MemoryEmbedding(Base):
    __tablename__ = "memory_embeddings"

    memory_id: Mapped[str] = mapped_column(
        String, ForeignKey("memories.id", ondelete="CASCADE"), primary_key=True
    )
    friend_id: Mapped[str] = mapped_column(
        String, ForeignKey("friends.id", ondelete="CASCADE"), nullable=False
    )
    model: Mapped[str] = mapped_column(String, nullable=False)
    dim: Mapped[int] = mapped_column(Integer, nullable=False)
    content_hash: Mapped[str] = mapped_column(String, nullable=False)
    vector: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    created_at: Mapped[str] = mapped_column(String, nullable=False)

    __table_args__ = (Index("idx_memory_embeddings_friend", "friend_id"),)
