"""知识库（v2.1 / T2.1-3 / D98）：每朋友一份资料库。

两张表：

```
documents        一份资料：元信息 + 抽出来的全文
document_chunks  切块（检索单位；原文一个字都不改）
```

**每朋友隔离**（跟记忆一样）：两张表都带 `friend_id`；`document_chunks.friend_id`
是冗余字段，检索时少一次 join。
"""

from sqlalchemy import ForeignKey, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from backend.db import Base


class Document(Base):
    __tablename__ = "documents"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    friend_id: Mapped[str] = mapped_column(
        String, ForeignKey("friends.id", ondelete="CASCADE"), nullable=False
    )
    name: Mapped[str] = mapped_column(String, nullable=False)  # 显示名（用户可改）
    kind: Mapped[str] = mapped_column(String, nullable=False)  # txt/md/pdf/docx/image
    path: Mapped[str | None] = mapped_column(String, nullable=True)  # 相对 user_data
    bytes: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    chars: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    text: Mapped[str | None] = mapped_column(Text, nullable=True)  # 抽出来的全文
    status: Mapped[str] = mapped_column(String, nullable=False, default="ready")  # ready/failed
    error: Mapped[str | None] = mapped_column(Text, nullable=True)  # 失败的人话原因
    created_at: Mapped[str] = mapped_column(String, nullable=False)
    updated_at: Mapped[str] = mapped_column(String, nullable=False)

    __table_args__ = (Index("idx_documents_friend", "friend_id"),)


class DocumentChunk(Base):
    __tablename__ = "document_chunks"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    document_id: Mapped[str] = mapped_column(
        String, ForeignKey("documents.id", ondelete="CASCADE"), nullable=False
    )
    friend_id: Mapped[str] = mapped_column(String, nullable=False)  # 冗余：检索少一次 join
    seq: Mapped[int] = mapped_column(Integer, nullable=False)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[str] = mapped_column(String, nullable=False)

    __table_args__ = (
        Index("idx_chunks_friend", "friend_id"),
        Index("idx_chunks_doc", "document_id", "seq"),
    )
