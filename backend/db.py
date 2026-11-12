"""数据库引擎与会话。

关键约定（data_model.md §7.2）：
  SQLite 默认 **不开启外键**，必须每次连接都 `PRAGMA foreign_keys=ON`，
  否则 `ON DELETE CASCADE` 形同虚设 —— 删朋友会留下一堆孤儿数据。
"""

from contextlib import contextmanager
import time
import traceback
from typing import Iterator
from pathlib import Path

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from loguru import logger

from backend.config import DB_PATH, ensure_dirs

# 2026-09-27（D197 续）：**长事务探针**。
# 现象：手机上"她一输出就断"，日志里 `database is locked` 反复出现 —— 说明**有个会话
# 把 SQLite 的写锁握着不撒手**。光看报错看不出是谁，所以这里把**开了超过这个秒数的会话**
# 记一条（带调用方），下次复现就知道抓谁。
LONG_TX_WARN_SECONDS = 5.0


class Base(DeclarativeBase):
    """所有 ORM 模型的基类。"""


_engine: Engine | None = None
_session_factory: sessionmaker[Session] | None = None


def _build_engine() -> Engine:
    ensure_dirs()
    engine = create_engine(f"sqlite:///{DB_PATH}", future=True)

    @event.listens_for(engine, "connect")
    def _enable_foreign_keys(dbapi_connection, _record) -> None:  # noqa: ANN001
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        # 2026-09-27（D195）：**WAL + busy_timeout** —— 不加的话"读写会互相卡死"：
        # 她回复在流式生成时占着一个长事务，手机端/后台任务任何一个写请求都会撞
        # `database is locked`（实测：手机连着聊天时 97 次"正文进度落库失败"，每 5.5 秒一次
        # = 5 秒 busy_timeout 白等完才失败）。
        # WAL 让"读"和"写"能同时进行（写不挡读、读不挡写）；busy_timeout 再兜一层。
        # 注意：WAL 是**写进库文件头的持久设置**，设一次就一直有效（每个连接设也无害）。
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA busy_timeout=5000")
        cursor.close()

    return engine


def get_engine() -> Engine:
    global _engine
    if _engine is None:
        _engine = _build_engine()
    return _engine


def get_session_factory() -> sessionmaker[Session]:
    global _session_factory
    if _session_factory is None:
        _session_factory = sessionmaker(bind=get_engine(), expire_on_commit=False, future=True)
    return _session_factory


@contextmanager
def session_scope() -> Iterator[Session]:
    """事务作用域：正常提交，异常回滚。"""
    session = get_session_factory()()
    started = time.monotonic()
    caller = "?"
    try:
        # ⚠️ 别用 `[-2]`：`@contextmanager` 会把 `contextlib.__enter__` 顶在栈上
        # （第一版就踩了这个坑，报出来的是 `contextlib.py:137 __enter__`，等于没说）。
        # 这里从栈顶往下**跳过 contextlib**，取第一个"真调用方"。
        stack = traceback.extract_stack()
        frame = next(
            (item for item in reversed(stack[:-1]) if "contextlib.py" not in item.filename),
            stack[-2],
        )
        caller = f"{Path(frame.filename).name}:{frame.lineno} {frame.name}"
    except Exception:  # noqa: BLE001 —— 探针自己不许影响业务
        pass
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        elapsed = time.monotonic() - started
        if elapsed >= LONG_TX_WARN_SECONDS:
            # 谁开着会话超过 5 秒谁就该被怀疑 —— 写锁很可能一直握在它手里
            logger.warning("长事务 {:.1f}s（{}）—— 期间 SQLite 的锁可能一直被它占着", elapsed, caller)
        session.close()
