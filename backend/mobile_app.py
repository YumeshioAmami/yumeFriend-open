"""Android 独立端的本地 FastAPI 应用。

与桌面 `backend.app` 的区别：
  · 不做 Alembic（APK 首次启动直接 `create_all`）；
  · 不启动桌面专属能力（游戏、看屏、审批、桌面语音服务）；
  · 前端静态文件从 APK assets 里取；
  · 只绑 127.0.0.1。
"""

from __future__ import annotations

import os
import asyncio
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncIterator

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from loguru import logger
from sqlalchemy import Boolean, Float, Integer, Numeric, String, Text, inspect
from sqlalchemy.engine import Engine

from backend.api import documents as documents_api
from backend.api import embedding as embedding_api
from backend.api import friends as friends_api
from backend.api import logs as logs_api
from backend.api import memories as memories_api
from backend.api import messages as messages_api
from backend.api import mobile_voice as mobile_voice_api
from backend.api import portraits as portraits_api
from backend.api import schedules as schedules_api
from backend.api import settings as settings_api
from backend.api import style as style_api
from backend.api import sync as sync_api
from backend.api import thoughts as thoughts_api
from backend.api import uploads as uploads_api
from backend.api import ws as ws_api
from backend.config import APP_NAME, APP_VERSION, ensure_dirs
from backend.db import Base, get_engine, session_scope
from backend.llm.errors import LLMConfigError
from backend.mobile_logging import configure as configure_mobile_logging
from backend.services.errors import ServiceError
from backend.services import server_sync, sync_service


def _quote_ident(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _column_default_sql(column) -> str | None:  # noqa: ANN001
    """给旧库补列时生成一个安全的 SQLite 默认值。"""
    default = getattr(getattr(column, "server_default", None), "arg", None)
    if default is None:
        default = getattr(getattr(column, "default", None), "arg", None)
    if default is not None and not callable(default):
        if isinstance(default, str):
            return "'" + default.replace("'", "''") + "'"
        if isinstance(default, bool):
            return "1" if default else "0"
        return str(default)
    if not column.nullable:
        if isinstance(column.type, (String, Text)):
            return "''"
        if isinstance(column.type, (Boolean, Integer, Float, Numeric)):
            return "0"
    return None


def _ensure_missing_columns(engine: Engine) -> None:
    """补齐 APK 跨版本升级时新增的列。

    手机端没有 Alembic；`create_all` 只建缺表，不会 ALTER 旧表。
    这里按 ORM metadata 检查每张已有表，缺什么列就 `ADD COLUMN`。
    这是 2026-11-14 手机端 HTTP 500 的根因修复：旧库缺
    `friends.visual_mode` / `messages.segments` 等列，查询整行直接抛
    `no such column`。
    """
    inspector = inspect(engine)
    existing_tables = set(inspector.get_table_names())
    added: list[str] = []
    with engine.begin() as conn:
        for table in Base.metadata.sorted_tables:
            if table.name not in existing_tables:
                continue
            existing_columns = {
                str(item["name"]) for item in inspector.get_columns(table.name)
            }
            for column in table.columns:
                if column.name in existing_columns:
                    continue
                type_sql = column.type.compile(dialect=engine.dialect)
                default_sql = _column_default_sql(column)
                ddl = (
                    f"ALTER TABLE {_quote_ident(table.name)} "
                    f"ADD COLUMN {_quote_ident(column.name)} {type_sql}"
                )
                if default_sql is not None:
                    ddl += f" DEFAULT {default_sql}"
                try:
                    conn.exec_driver_sql(ddl)
                    added.append(f"{table.name}.{column.name}")
                except Exception as exc:  # noqa: BLE001
                    # 并发启动 / 重复迁移时可能已经被另一条连接补上。
                    if "duplicate column name" not in str(exc).lower():
                        raise
        # 补列后把索引也补上；缺索引只影响性能，但顺手修掉。
        for table in Base.metadata.sorted_tables:
            if table.name not in existing_tables:
                continue
            for index in table.indexes:
                try:
                    index.create(bind=conn, checkfirst=True)
                except Exception as exc:  # noqa: BLE001
                    logger.warning("手机端补索引失败（{}）：{}", index.name, exc)
    if added:
        logger.info("手机数据库已补齐 {} 个缺失列：{}", len(added), ", ".join(added))


def _create_tables() -> None:
    # 必须先 import 全部模型，metadata 才完整。
    from backend import models  # noqa: F401, PLC0415

    engine = get_engine()
    # 先建缺失的表，再做轻量迁移。
    # ⚠️ 顺序不能反：全新安装时 messages 还不存在，后面那条
    # `UPDATE messages SET updated_at = ...` 会直接报 no such table: messages，
    # Kotlin 端就会一直卡在“正在启动手机本地服务…”。
    Base.metadata.create_all(bind=engine)
    # 轻量迁移：APK 升级时 create_all 不会 ALTER 已有表；这里补新增列。
    with engine.begin() as conn:
        cols = {row[1] for row in conn.exec_driver_sql("PRAGMA table_info(settings)")}
        if cols and "autonomy_owner_device_id" not in cols:
            conn.exec_driver_sql(
                "ALTER TABLE settings ADD COLUMN autonomy_owner_device_id VARCHAR"
            )
        friend_cols = {row[1] for row in conn.exec_driver_sql("PRAGMA table_info(friends)")}
        if friend_cols and "life_enabled" not in friend_cols:
            conn.exec_driver_sql(
                "ALTER TABLE friends ADD COLUMN life_enabled INTEGER NOT NULL DEFAULT 0"
            )
        if friend_cols and "chat_pause_until" not in friend_cols:
            conn.exec_driver_sql("ALTER TABLE friends ADD COLUMN chat_pause_until VARCHAR")
        if friend_cols and "chat_pacing_enabled" not in friend_cols:
            conn.exec_driver_sql(
                "ALTER TABLE friends ADD COLUMN chat_pacing_enabled INTEGER NOT NULL DEFAULT 0"
            )
        if friend_cols and "humanizer_enabled" not in friend_cols:
            conn.exec_driver_sql(
                "ALTER TABLE friends ADD COLUMN humanizer_enabled INTEGER NOT NULL DEFAULT 0"
            )
        message_cols = {row[1] for row in conn.exec_driver_sql("PRAGMA table_info(messages)")}
        if message_cols and "origin_device_id" not in message_cols:
            conn.exec_driver_sql("ALTER TABLE messages ADD COLUMN origin_device_id VARCHAR")
        if message_cols and "updated_at" not in message_cols:
            conn.exec_driver_sql("ALTER TABLE messages ADD COLUMN updated_at VARCHAR")
        conn.exec_driver_sql(
            "UPDATE messages SET updated_at = created_at "
            "WHERE updated_at IS NULL OR updated_at = ''"
        )
        # D382：不要每次启动都把最近助手消息重新标脏。那会让手机只补传
        # assistant、不补传对应的 user，正是“她连续说话”那类同步事故的源头。
        life_cols = {row[1] for row in conn.exec_driver_sql("PRAGMA table_info(friend_life)")}
        if life_cols and "pending_share" not in life_cols:
            conn.exec_driver_sql(
                "ALTER TABLE friend_life ADD COLUMN pending_share TEXT NOT NULL DEFAULT ''"
            )
        if life_cols and "last_share_at" not in life_cols:
            conn.exec_driver_sql("ALTER TABLE friend_life ADD COLUMN last_share_at VARCHAR")
        activity_cols = {
            row[1] for row in conn.exec_driver_sql("PRAGMA table_info(life_activities)")
        }
        if activity_cols and "project_id" not in activity_cols:
            conn.exec_driver_sql("ALTER TABLE life_activities ADD COLUMN project_id VARCHAR")
    # 兼容所有历史版本：以后再加字段也不需要重新手写一遍 ALTER。
    _ensure_missing_columns(engine)


@asynccontextmanager
async def mobile_lifespan(_app: FastAPI) -> AsyncIterator[None]:
    logger.info("yumeFriend 手机本地服务已启动")
    from backend.mobile_scheduler import build_mobile_scheduler, register_notifications
    from backend.services import settings_service, voice_reply_service

    voice_reply_service.bind_loop(asyncio.get_running_loop())
    register_notifications()
    with session_scope() as session:
        frequency = settings_service.get(session).active_message_frequency
    scheduler = build_mobile_scheduler(heartbeat_minutes=frequency)
    scheduler.start()
    _app.state.scheduler = scheduler
    server_sync.start_loop()
    try:
        yield
    finally:
        from backend.agent import reply_runs  # noqa: PLC0415

        await server_sync.stop_loop()
        scheduler.shutdown(wait=False)
        await reply_runs.shutdown()
        logger.info("yumeFriend 手机本地服务已停止")


def create_mobile_app() -> FastAPI:
    ensure_dirs()
    configure_mobile_logging()
    _create_tables()
    with session_scope() as session:
        sync_service.configure_device(
            session,
            name=os.environ.get("YF_DEVICE_NAME") or "yumeFriend 手机",
            kind=os.environ.get("YF_DEVICE_KIND") or "phone",
        )
        server_sync.ensure_paused_on_upgrade(session)

    app = FastAPI(title=f"{APP_NAME}-mobile", version=APP_VERSION, lifespan=mobile_lifespan)

    @app.exception_handler(ServiceError)
    async def _service_error_handler(_request: Request, exc: ServiceError) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status,
            content={"error": {"code": exc.code, "message": exc.message}},
        )

    @app.exception_handler(LLMConfigError)
    async def _llm_config_error_handler(_request: Request, exc: LLMConfigError) -> JSONResponse:
        return JSONResponse(
            status_code=400,
            content={"error": {"code": "LLM_NOT_CONFIGURED", "message": str(exc)}},
        )

    @app.exception_handler(ValueError)
    async def _value_error_handler(_request: Request, exc: ValueError) -> JSONResponse:
        return JSONResponse(
            status_code=422,
            content={"error": {"code": "INVALID_INPUT", "message": str(exc)}},
        )

    @app.get("/api/health")
    async def health() -> dict:
        return {"ok": True, "app": APP_NAME, "version": APP_VERSION, "platform": "android"}

    app.include_router(documents_api.router)
    app.include_router(embedding_api.router)
    app.include_router(portraits_api.router)
    app.include_router(friends_api.router)
    app.include_router(logs_api.router)
    app.include_router(memories_api.router)
    app.include_router(messages_api.router)
    app.include_router(mobile_voice_api.router)
    app.include_router(schedules_api.router)
    app.include_router(settings_api.router)
    app.include_router(style_api.router)
    app.include_router(sync_api.router)
    app.include_router(thoughts_api.router)
    app.include_router(uploads_api.router)
    app.include_router(ws_api.router)

    # APK 内置前端。必须最后挂，避免 /api 被静态路由抢走。
    web_dir = Path(os.environ.get("YF_MOBILE_WEB_DIR", ""))
    if web_dir.is_dir():
        app.mount("/", StaticFiles(directory=str(web_dir), html=True), name="mobile-web")

    return app
