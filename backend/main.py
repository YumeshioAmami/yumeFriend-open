"""入口：启动本地后端。

用法：python -m backend.main     （见 AGENTS.md §6）
"""

import uvicorn
from loguru import logger
import faulthandler

from backend import logbuffer
from backend.config import HOST, LOG_DIR, PORT

_crash_stream = None


def configure_logging() -> None:
    """日志落盘（D31 的日志窗口 M6 才做，但**报错必须能事后查** —— 现在就要）。

    位置：user_data/logs/yumefriend.log（已 gitignore）；5 MB 轮转，留 5 份。
    注意：日志**不记聊天明文**（LLM协议§9 第 6 条），API key 也永不记。
    """
    logger.add(
        LOG_DIR / "yumefriend.log",
        rotation="5 MB",
        retention=5,
        encoding="utf-8",
        enqueue=True,
        backtrace=False,
        diagnose=False,
    )
    # 日志窗口要的实时缓冲（D31：双写 —— 内存给窗口，文件给导出/追溯）
    logger.add(logbuffer.sink, level="DEBUG")
    # 闪退诊断：Python 层硬崩（段错误 / abort）时把线程栈写进 faulthandler.log。
    global _crash_stream
    try:
        _crash_stream = open(LOG_DIR / "faulthandler.log", "a", encoding="utf-8")
        faulthandler.enable(_crash_stream, all_threads=True)
    except Exception:
        # 诊断失败绝不能挡住启动
        pass


def main() -> None:
    configure_logging()
    # 只绑 127.0.0.1：一旦绑 0.0.0.0，同局域网任何人都能读走全部聊天记录（api.md §0.2）
    uvicorn.run("backend.app:app", host=HOST, port=PORT, reload=False)


if __name__ == "__main__":
    main()
