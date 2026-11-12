"""路由共用依赖。"""

from typing import Callable, Iterator

from fastapi import Depends
from sqlalchemy.orm import Session

from backend.db import get_session_factory
from backend.llm.base import ILLMProvider
from backend.llm.factory import create_provider, create_provider_for_llm_base
from backend.services import settings_service


def get_db() -> Iterator[Session]:
    """请求级数据库会话：正常提交，异常回滚。"""
    session = get_session_factory()()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def get_provider_factory(session: Session = Depends(get_db)) -> Callable[..., ILLMProvider]:
    """返回一个"造 provider"的函数，**而不是直接造**。

    为什么要拐这一下：FastAPI 的依赖在进入端点函数体之前就全部解析完了。
    如果直接把 provider 作为依赖，那么"朋友不存在"（404）会先被
    "没配 provider"（400）挡掉 —— 顺序反了。这里让端点自己挑时候调用。

    配置不全时抛 LLMConfigError → API 层转成 400 `LLM_NOT_CONFIGURED`（api.md §9）。

    v3.5（D151）：可以带一个"预设名"调用 —— 那是**这个朋友自己指定的模型端点**
    （`friends.llm_base`）；不带就还是全局「聊天」用途。
    """

    def _make(base_name: str | None = None) -> ILLMProvider:
        if base_name and base_name.strip():
            row = settings_service.get(session)
            return create_provider_for_llm_base(row.llm_bases, base_name)
        bases, active = settings_service.llm_config(session)
        return create_provider(bases, active)

    return _make
