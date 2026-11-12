"""手机独立端日志必须同时进文件和 `/api/logs` 的内存缓冲（D309）。"""

from __future__ import annotations

from loguru import logger

from backend import logbuffer
from backend import mobile_logging


def test_mobile_logging_writes_to_file_and_buffer(tmp_path):
    log_dir = tmp_path / "logs"
    logbuffer.clear()
    mobile_logging.configure(log_dir)

    logger.info("手机日志测试 {}", "ok")

    items, _ = logbuffer.snapshot()
    assert any("手机日志测试 ok" in item["message"] for item in items)
    assert (log_dir / "yumefriend.log").is_file()


def test_mobile_logging_is_idempotent(tmp_path):
    log_dir = tmp_path / "logs"
    mobile_logging.configure(log_dir)
    first = list(mobile_logging._handler_ids)

    mobile_logging.configure(log_dir)

    assert mobile_logging._handler_ids == first
    assert len(mobile_logging._handler_ids) == 2
