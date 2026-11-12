"""知识库（v2.1 / T2.1-3 / D98）：收文件 → 抽文字 → 切块 → 存 → 按关键词找回来。

**每朋友一份、完全隔离**；**不上向量库**（v2_scope 的口径）——检索用关键词打分：
中文按 2-gram/3-gram 切词、拉丁按空格，块得分 = Σ（词出现次数 × 词长）。

支持的类型：

```
txt / md   直接读（自动认 UTF-16 / UTF-8 / GB18030，见 `backend/text_encoding.py`）
pdf        pypdf
docx       python-docx
image      走现成的视觉链路（chat/vision 模型）；没配视觉 → status=failed + 人话
```

三条纪律：

· **抽不出来不是错误**（D83）：留 `status=failed` + 一句人话，用户能看到、能删
· **原文一个字都不改**：切块只是检索单位，不是"压缩记忆"
· **资料 ≠ 记忆**：这里存的东西不进 `memories` 表
"""

from __future__ import annotations

import base64
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path

from loguru import logger
from sqlalchemy import select
from sqlalchemy.orm import Session

from backend import config
from backend.llm.errors import LLMConfigError
from backend.llm.factory import create_provider_for_purpose
from backend.models import Document, DocumentChunk, Friend
from backend.services import settings_service
from backend.services.errors import NotFoundError, ServiceError
from backend.text_encoding import decode_text

MAX_UPLOAD_BYTES = 30 * 1024 * 1024  # 单份 30MB
MAX_STORE_CHARS = 400_000  # 再长就截断（个人资料够用；别把库撑爆）
CHUNK_CHARS = 600
CHUNK_OVERLAP = 80
RETRIEVE_LIMIT = 3
RETRIEVE_MAX_CHARS = 1200
# 朋友的资料**总共**不超过这么多字 → 干脆**全给**（关键词检索会被同义改写绕过：
# 问「我那只猫叫什么」、原文写「养了一只叫橘子的猫」→ n-gram 一个都对不上，实测踩过）
SMALL_KB_CHARS = 1200

_TEXT_KINDS = {"txt": "txt", "md": "md", "markdown": "md"}
_PDF_KINDS = {"pdf": "pdf"}
_DOC_KINDS = {"docx": "docx"}
_IMAGE_KINDS = {"png": "image", "jpg": "image", "jpeg": "image", "webp": "image", "gif": "image"}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _new_id() -> str:
    return str(uuid.uuid4())


def kind_of(filename: str) -> str | None:
    suffix = Path(filename or "").suffix.lower().lstrip(".")
    return (
        _TEXT_KINDS.get(suffix)
        or _PDF_KINDS.get(suffix)
        or _DOC_KINDS.get(suffix)
        or _IMAGE_KINDS.get(suffix)
    )


# ---------- 抽取 ----------


def _read_text(raw: bytes) -> str:
    """txt/md 的字节 → 文字（编码探测收在 `backend/text_encoding.py`，文件工具共用）。"""
    return decode_text(raw)


def _extract_pdf(raw: bytes) -> str:
    from pypdf import PdfReader  # noqa: PLC0415 —— 只有真要读 pdf 才 import

    import io

    reader = PdfReader(io.BytesIO(raw))
    pages = []
    for page in reader.pages[:400]:  # 防超长 PDF 卡死
        try:
            pages.append(page.extract_text() or "")
        except Exception as exc:  # noqa: BLE001 —— 单页坏了别放弃整份
            logger.warning("PDF 某一页抽不出来（跳过）：{}", exc)
    return "\n".join(pages)


def _extract_docx(raw: bytes) -> str:
    import io

    from docx import Document as DocxDocument  # noqa: PLC0415

    document = DocxDocument(io.BytesIO(raw))
    lines = [paragraph.text for paragraph in document.paragraphs]
    for table in document.tables:
        for row in table.rows:
            lines.append(" | ".join(cell.text for cell in row.cells))
    return "\n".join(lines)


IMAGE_PROMPT = """这是一张 TA 给你的资料图。请**如实**把它转成文字：

- 有文字就照抄（保留段落/标题的层次感），别翻译、别改写
- 是图表/截图/照片就把里面的关键信息写清楚（表格按行写、界面写了什么）
- 看不清、认不出的地方就写「（看不清）」，**不许猜**
只输出内容本身，别加"这张图是……"这类说明。"""


async def _extract_image(raw: bytes, *, session: Session, filename: str) -> str:
    purposes = settings_service.effective_purposes_from(settings_service.get(session))
    import json

    try:
        provider = create_provider_for_purpose(
            json.dumps(purposes, ensure_ascii=False), "vision"
        )
    except LLMConfigError as exc:
        raise ServiceError(
            "VISION_NOT_READY",
            f"图片资料要配「视觉」模型才能读（{exc}）—— 去设置里配一个，或者先把文字版发给她",
            400,
        ) from exc
    encoded = base64.b64encode(raw).decode()
    suffix = Path(filename).suffix.lower().lstrip(".") or "png"
    media = "image/jpeg" if suffix in ("jpg", "jpeg") else f"image/{suffix}"
    messages = [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": IMAGE_PROMPT},
                {"type": "image_url", "image_url": {"url": f"data:{media};base64,{encoded}"}},
            ],
        }
    ]
    chunks: list[str] = []
    try:
        async for event in provider.chat_stream(messages, temperature=0.2, max_tokens=1500):
            if event.get("type") == "content":
                chunks.append(event.get("delta") or "")
            elif event.get("type") == "done":
                break
    finally:
        closer = getattr(provider, "aclose", None)
        if closer is not None:
            await closer()
    return "".join(chunks).strip()


def extract_sync(kind: str, raw: bytes, *, filename: str) -> str:
    """同步抽取（txt/md/pdf/docx）。图片走 `extract_image`（要 await）。"""
    if kind in ("txt", "md"):
        return _read_text(raw)
    if kind == "pdf":
        return _extract_pdf(raw)
    if kind == "docx":
        return _extract_docx(raw)
    raise ServiceError("DOC_KIND_UNSUPPORTED", f"还不支持这种文件：{kind}", 400)


# ---------- 切块 / 检索 ----------


def chunk_text(text: str, *, size: int = CHUNK_CHARS, overlap: int = CHUNK_OVERLAP) -> list[str]:
    """按段落切，尽量在段落边界收口；太长就硬切。**一个字都不改。**"""
    cleaned = re.sub(r"\r\n?", "\n", text or "").strip()
    if not cleaned:
        return []
    paragraphs = [p.strip() for p in re.split(r"\n{2,}", cleaned) if p.strip()]
    chunks: list[str] = []
    buffer = ""
    for paragraph in paragraphs:
        while len(paragraph) > size:  # 单段就超长 → 硬切
            if buffer:
                chunks.append(buffer)
                buffer = ""
            chunks.append(paragraph[:size])
            paragraph = paragraph[size - overlap :]
        if len(buffer) + len(paragraph) + 1 <= size:
            buffer = f"{buffer}\n{paragraph}".strip()
        else:
            if buffer:
                chunks.append(buffer)
            buffer = paragraph
    if buffer:
        chunks.append(buffer)
    return chunks


def _terms(query: str) -> list[str]:
    """中文 2-gram/3-gram + 拉丁词。"""
    text = (query or "").lower()
    terms: list[str] = []
    for word in re.findall(r"[a-z0-9]{2,}", text):
        terms.append(word)
    for block in re.findall(r"[\u4e00-\u9fff]{2,}", text):
        for size in (2, 3):
            for index in range(len(block) - size + 1):
                terms.append(block[index : index + size])
    # 去重但保留顺序，去掉被更长词包含的短词（省点计算）
    unique: list[str] = []
    for term in terms:
        if term not in unique:
            unique.append(term)
    return unique[:60]


def _score(terms: list[str], text: str) -> float:
    lowered = text.lower()
    return float(sum(lowered.count(term) * len(term) for term in terms))


def retrieve(
    session: Session,
    friend_id: str,
    query: str,
    *,
    limit: int = RETRIEVE_LIMIT,
    max_chars: int = RETRIEVE_MAX_CHARS,
) -> list[dict]:
    """按关键词找出**最相关的几块**（资料 ≠ 记忆；只进本轮上下文）。"""
    terms = _terms(query)
    if not terms:
        return []
    rows = list(
        session.execute(select(DocumentChunk).where(DocumentChunk.friend_id == friend_id)).scalars()
    )
    names = {
        doc.id: doc.name
        for doc in session.execute(
            select(Document).where(Document.friend_id == friend_id)
        ).scalars()
    }
    scored = [(_score(terms, row.text), row) for row in rows]
    scored = [item for item in scored if item[0] > 0]
    scored.sort(key=lambda item: item[0], reverse=True)

    hits: list[dict] = []
    used = 0
    seen_docs: set[str] = set()
    for score, row in scored:
        if len(hits) >= limit or used >= max_chars:
            break
        # 同一份资料最多给两块（免得一整轮全是同一份文件的相邻段落）
        if list(r.document_id for _, r in scored[: len(hits) + 1]).count(row.document_id) > 2:
            continue
        if len(row.text) + used > max_chars and hits:
            continue
        hits.append(
            {
                "document_id": row.document_id,
                "document_name": names.get(row.document_id, "（资料）"),
                "seq": row.seq,
                "text": row.text,
                "score": round(score, 1),
            }
        )
        used += len(row.text)
        seen_docs.add(row.document_id)
    return hits


# ---------- CRUD ----------


def document_index(session: Session, friend_id: str) -> list[dict]:
    """资料清单（只给名字 + 字数；很便宜，每轮都能带上）。"""
    rows = session.execute(
        select(Document).where(Document.friend_id == friend_id).order_by(Document.created_at)
    ).scalars()
    return [
        {"id": doc.id, "name": doc.name, "kind": doc.kind, "chars": doc.chars, "status": doc.status}
        for doc in rows
    ]


def small_texts(session: Session, friend_id: str, *, max_total: int = SMALL_KB_CHARS) -> list[dict]:
    """资料都很短（总共 ≤ `max_total`）时，直接把全文给她 —— 比关键词检索可靠得多。

    返回 `[{"name": ..., "text": ...}]`；**超过阈值就返回空**（那就走关键词检索 + 工具）。
    """
    rows = list(
        session.execute(
            select(Document)
            .where(Document.friend_id == friend_id, Document.status == "ready")
            .order_by(Document.created_at)
        ).scalars()
    )
    total = sum(len(doc.text or "") for doc in rows)
    if not rows or total > max_total:
        return []
    return [{"name": doc.name, "text": (doc.text or "").strip()} for doc in rows if doc.text]


def list_documents(session: Session, friend_id: str) -> list[dict]:
    rows = session.execute(
        select(Document).where(Document.friend_id == friend_id).order_by(Document.created_at.desc())
    ).scalars()
    return [to_dict(doc) for doc in rows]


def get_document(session: Session, friend_id: str, document_id: str) -> Document:
    document = session.get(Document, document_id)
    if document is None or document.friend_id != friend_id:
        raise NotFoundError("DOCUMENT_NOT_FOUND", "找不到这份资料")
    return document


def to_dict(document: Document, *, with_text: bool = False) -> dict:
    data = {
        "id": document.id,
        "friend_id": document.friend_id,
        "name": document.name,
        "kind": document.kind,
        "bytes": document.bytes,
        "chars": document.chars,
        "status": document.status,
        "error": document.error,
        "created_at": document.created_at,
        "updated_at": document.updated_at,
    }
    if with_text:
        data["text"] = document.text or ""
    return data


def rename(session: Session, friend_id: str, document_id: str, name: str) -> Document:
    cleaned = (name or "").strip()
    if not cleaned:
        raise ValueError("名字不能是空的")
    document = get_document(session, friend_id, document_id)
    document.name = cleaned[:120]
    document.updated_at = _now()
    session.flush()
    return document


def update_text(session: Session, friend_id: str, document_id: str, text: str) -> Document:
    """用户**手改抽取出来的文字**（改完重新切块）。原件文件不动。

    为什么要有这个：扫描件/OCR 出来的文字总有小错，让用户能就地修 —— 比"重新传一份"
    友好得多（T2.1-6 用户报的"知识库没法删改"）。
    """
    document = get_document(session, friend_id, document_id)
    body = (text or "").strip()
    if len(body) > MAX_STORE_CHARS:
        raise ValueError(f"太长了（最多 {MAX_STORE_CHARS} 字）")
    document.text = body or None
    document.chars = len(body)
    document.status = "ready" if body else "failed"
    document.error = None if body else "内容是空的（用户清掉了）"
    document.updated_at = _now()
    # 重新切块：旧块全删，按新文字重建（检索用的就是这些块）
    for chunk in session.execute(
        select(DocumentChunk).where(DocumentChunk.document_id == document_id)
    ).scalars():
        session.delete(chunk)
    for index, piece in enumerate(chunk_text(body)):
        session.add(
            DocumentChunk(
                id=_new_id(),
                document_id=document_id,
                friend_id=friend_id,
                seq=index,
                text=piece,
                created_at=_now(),
            )
        )
    session.flush()
    logger.info("资料内容被用户改过：{}（现在 {} 字）", document.name, document.chars)
    return document


def delete(session: Session, friend_id: str, document_id: str) -> None:
    """删资料：**行 + 原件文件一起删**（宪法第九条：删除要彻底）。"""
    document = get_document(session, friend_id, document_id)
    from backend.services import sync_service  # noqa: PLC0415

    sync_service.tombstone(session, "documents", document_id)
    path = abs_path(document.path)
    session.delete(document)  # 块由 FK CASCADE 一起走
    session.flush()
    if path is not None:
        try:
            path.unlink()
        except OSError as exc:
            logger.warning("资料文件删不掉（忽略）：{}", exc)


def abs_path(rel_path: str | None) -> Path | None:
    """`documents.path`（相对 user_data）→ 真实文件；**挡路径穿越**。"""
    if not rel_path:
        return None
    base = config.USER_DATA_DIR.resolve()
    path = (config.USER_DATA_DIR / rel_path).resolve()
    if path != base and base not in path.parents:
        return None
    return path if path.is_file() else None


def clear_files_for_friend(session: Session, friend_id: str) -> None:
    """删朋友时把原件也清掉（D98 第 7 条）。"""
    rows = session.execute(select(Document).where(Document.friend_id == friend_id)).scalars()
    for document in list(rows):
        path = abs_path(document.path)
        if path is not None:
            try:
                path.unlink()
            except OSError:
                pass


def create(
    session: Session,
    friend_id: str,
    *,
    filename: str,
    raw: bytes,
    text: str | None = None,
    error: str | None = None,
) -> Document:
    """建一条资料记录（`text` 给了就直接用，否则调用方先抽好）。"""
    if session.get(Friend, friend_id) is None:
        raise NotFoundError("FRIEND_NOT_FOUND", "找不到这个朋友")
    if not raw:
        raise ServiceError("DOC_EMPTY", "这个文件是空的", 400)
    if len(raw) > MAX_UPLOAD_BYTES:
        raise ServiceError(
            "DOC_TOO_LARGE", f"文件太大了（最大 {MAX_UPLOAD_BYTES // 1024 // 1024} MB）", 400
        )
    kind = kind_of(filename)
    if kind is None:
        raise ServiceError(
            "DOC_KIND_UNSUPPORTED",
            "现在支持 txt / md / pdf / docx / 图片（png jpg webp gif）",
            400,
        )

    document_id = _new_id()
    suffix = Path(filename).suffix.lower() or ".bin"
    rel = f"documents/{document_id}{suffix}"
    config.DOCUMENT_DIR.mkdir(parents=True, exist_ok=True)
    try:
        (config.DOCUMENT_DIR / f"{document_id}{suffix}").write_bytes(raw)
    except OSError as exc:
        raise ServiceError("DOC_WRITE_FAILED", "文件存不下来（磁盘？）", 500) from exc

    body = (text or "").strip()
    if len(body) > MAX_STORE_CHARS:
        body = body[:MAX_STORE_CHARS]
        logger.info("资料太长，只留前 {} 字：{}", MAX_STORE_CHARS, filename)

    now = _now()
    document = Document(
        id=document_id,
        friend_id=friend_id,
        name=Path(filename).name[:120] or "资料",
        kind=kind,
        path=rel,
        bytes=len(raw),
        chars=len(body),
        text=body or None,
        status="failed" if error else "ready",
        error=error,
        created_at=now,
        updated_at=now,
    )
    session.add(document)
    session.flush()

    for index, piece in enumerate(chunk_text(body)):
        session.add(
            DocumentChunk(
                id=_new_id(),
                document_id=document_id,
                friend_id=friend_id,
                seq=index,
                text=piece,
                created_at=now,
            )
        )
    session.flush()
    logger.info(
        "资料入库：{}（{}，{} 字，{} 块）",
        document.name,
        kind,
        len(body),
        max(0, len(chunk_text(body))),
    )
    return document
