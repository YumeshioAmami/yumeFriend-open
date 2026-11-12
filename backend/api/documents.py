"""知识库（v2.1 / T2.1-3；api.md 的【v2.1】2）。

上传后**同步抽取**（大 PDF 要几秒 —— 端点用 async + `to_thread` 跑抽取，不堵事件循环）。
抽不出文字**不是错误**：留 `status=failed` + 一句人话（D83）。
"""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, Depends, File, Response, UploadFile
from loguru import logger
from sqlalchemy.orm import Session

from backend.api.deps import get_db
from backend.services import friend_service, knowledge_service
from backend.services.actor import Actor
from backend.services.errors import ServiceError

router = APIRouter(prefix="/api/friends", tags=["documents"])


@router.post("/{friend_id}/documents")
async def add_document(
    friend_id: str,
    file: UploadFile = File(...),
    session: Session = Depends(get_db),
) -> dict:
    friend_service.get(session, Actor.USER, friend_id)
    raw = await file.read()
    filename = file.filename or "资料"
    kind = knowledge_service.kind_of(filename)
    if kind is None:
        raise ServiceError(
            "DOC_KIND_UNSUPPORTED", "现在支持 txt / md / pdf / docx / 图片", 400
        )

    text = ""
    error: str | None = None
    if kind == "image":
        try:
            text = await knowledge_service._extract_image(  # noqa: SLF001 —— 同一模块的服务
                raw, session=session, filename=filename
            )
        except ServiceError as exc:
            error = exc.message
        except Exception as exc:  # noqa: BLE001 —— 读图失败也是"这份读不出来"
            logger.warning("图片资料读不出来：{}", exc)
            error = "这张图读不出来（模型没连上？）—— 传文字版更保险"
    else:
        try:
            text = await asyncio.to_thread(
                knowledge_service.extract_sync, kind, raw, filename=filename
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("资料抽取失败（{}）：{}", filename, exc)
            error = f"这份文件读不出文字（{type(exc).__name__}）—— 可能是扫描件或加密的"
    if not error and not text.strip():
        error = "没能从这份文件里读出文字（扫描件 / 纯图片 PDF？）—— 可以试试把它截图发给她"

    document = knowledge_service.create(
        session, friend_id, filename=filename, raw=raw, text=text, error=error
    )
    session.commit()  # 先提交，再让前端看到（D94）
    return knowledge_service.to_dict(document)


@router.get("/{friend_id}/documents")
def list_documents(friend_id: str, session: Session = Depends(get_db)) -> dict:
    friend_service.get(session, Actor.USER, friend_id)
    return {"documents": knowledge_service.list_documents(session, friend_id)}


@router.get("/{friend_id}/documents/{document_id}")
def get_document(friend_id: str, document_id: str, session: Session = Depends(get_db)) -> dict:
    friend_service.get(session, Actor.USER, friend_id)
    document = knowledge_service.get_document(session, friend_id, document_id)
    return knowledge_service.to_dict(document, with_text=True)


@router.patch("/{friend_id}/documents/{document_id}")
def update_document(
    friend_id: str, document_id: str, body: dict, session: Session = Depends(get_db)
) -> dict:
    friend_service.get(session, Actor.USER, friend_id)
    document = knowledge_service.get_document(session, friend_id, document_id)
    if "name" in body:
        document = knowledge_service.rename(
            session, friend_id, document_id, str(body.get("name") or "")
        )
    if "text" in body:
        # 用户手改抽取出来的文字（改完重新切块；原件不动）
        document = knowledge_service.update_text(
            session, friend_id, document_id, str(body.get("text") or "")
        )
    session.commit()
    return knowledge_service.to_dict(document)


@router.delete(
    "/{friend_id}/documents/{document_id}",
    status_code=204,
    response_class=Response,
    response_model=None,
)
def delete_document(friend_id: str, document_id: str, session: Session = Depends(get_db)) -> None:
    friend_service.get(session, Actor.USER, friend_id)
    knowledge_service.delete(session, friend_id, document_id)
    session.commit()


@router.post("/{friend_id}/documents/search")
def search_documents(friend_id: str, body: dict, session: Session = Depends(get_db)) -> dict:
    friend_service.get(session, Actor.USER, friend_id)
    query = str(body.get("query") or "").strip()
    if not query:
        raise ValueError("要搜什么？")
    return {"hits": knowledge_service.retrieve(session, friend_id, query, limit=5)}
