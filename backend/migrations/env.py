"""Alembic 环境：指向 backend.db 的 metadata 与本地 SQLite（user_data/yumefriend.db）。

注意：这里用 alembic 自己建的 engine（不带 PRAGMA foreign_keys=ON），
迁移期间外键关闭是 SQLite 的常规做法；业务代码走 backend/db.py 的 engine（强制开启）。
"""
from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

import backend.models  # noqa: F401  必须导入，否则 autogenerate 看不到表
from backend.config import DB_PATH, ensure_dirs
from backend.db import Base

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def _url() -> str:
    ensure_dirs()
    return f"sqlite:///{DB_PATH}"


def run_migrations_offline() -> None:
    context.configure(
        url=_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        render_as_batch=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    section = config.get_section(config.config_ini_section) or {}
    section["sqlalchemy.url"] = _url()
    connectable = engine_from_config(section, prefix="sqlalchemy.", poolclass=pool.NullPool)
    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata, render_as_batch=True)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()