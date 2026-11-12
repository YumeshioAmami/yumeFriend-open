"""打包后的入口（PyInstaller 用）：双击 exe 该干的事 = 启动桌面版。

跟 `python -m backend.desktop.app` 是同一个入口，只是多两层打包专属的照顾：

1. **窗口模式下 `sys.stdout/stderr` 是 None** —— 有些库（uvicorn 的日志配置就干过）
   会因此直接炸；这里给它们一个文件兜底。
2. **未捕获异常写 `user_data/logs/crash.log`**，再弹一句人话 ——
   不然 PyInstaller 只会弹一个"Unhandled exception in script"对话框，
   用户看不到原因，而且**那个框会把进程挂在那儿**（2026-09-21 实测踩到）。
"""

import multiprocessing
import sys
import traceback


def _log_dir():  # noqa: ANN202 —— 返回 Path，这里不 import 是为了异常也能写日志
    from backend.config import LOG_DIR

    LOG_DIR.mkdir(parents=True, exist_ok=True)
    return LOG_DIR


def _prepare_streams() -> None:
    """窗口模式（console=False）下把 stdout/stderr 指到一个文件，别让它们当 None。"""
    if sys.stdout is not None and sys.stderr is not None:
        return
    try:
        stream = _log_dir().joinpath("stdout.log").open("a", encoding="utf-8", buffering=1)
    except OSError:
        return
    if sys.stdout is None:
        sys.stdout = stream
    if sys.stderr is None:
        sys.stderr = stream


def run() -> int:
    from backend.desktop.app import main

    return int(main(sys.argv[1:]) or 0)


if __name__ == "__main__":
    multiprocessing.freeze_support()  # PyInstaller 的老规矩：万一有子进程
    _prepare_streams()
    try:
        code = run()
    except Exception:  # noqa: BLE001 —— 起不来也要留下证据 + 说人话
        detail = traceback.format_exc()
        path = None
        try:
            path = _log_dir().joinpath("crash.log")
            path.write_text(detail, encoding="utf-8")
        except Exception:  # noqa: BLE001
            pass
        try:
            from backend.desktop.app import _message_box  # type: ignore[attr-defined]

            _message_box(
                "yumeFriend 起不来（启动就出错了）。\n\n"
                f"把这份文件发给开发者：{path}\n\n"
                + detail.strip().splitlines()[-1][:300]
            )
        except Exception:  # noqa: BLE001
            pass
        sys.exit(1)
    sys.exit(code)
