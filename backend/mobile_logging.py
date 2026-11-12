"""手机独立端的日志配置（D309）。

桌面入口在 `backend.main.configure_logging()`，手机入口走 Chaquopy，不会经过那里。
这里补同一套双写：文件用于事后追溯，`logbuffer.sink` 供 `/api/logs` 日志页实时读取。
"""

from __future__ import annotations

from pathlib import Path

from loguru import logger

from backend import config, logbuffer

_target: Path | None = None
_handler_ids: list[int] = []


def configure(log_dir: str | Path | None = None) -> Path:
    """幂等配置手机日志；返回日志目录。

    测试或同一进程切换数据目录时，会先移除本模块此前添加的 handler，避免重复行。
    """
    global _target

    target = Path(log_dir or config.LOG_DIR)
    target.mkdir(parents=True, exist_ok=True)
    if _target == target and _handler_ids:
        return target

    for handler_id in _handler_ids:
        try:
            logger.remove(handler_id)
        except ValueError:
            pass
    _handler_ids.clear()

    file_id = logger.add(
        target / "yumefriend.log",
        rotation="5 MB",
        retention=5,
        encoding="utf-8",
        backtrace=False,
        diagnose=False,
    )
    buffer_id = logger.add(logbuffer.sink, level="DEBUG")
    _handler_ids.extend((file_id, buffer_id))
    _target = target
    logger.info("手机端日志已接入：{}", target / "yumefriend.log")
    return target
