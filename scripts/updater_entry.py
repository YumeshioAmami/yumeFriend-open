"""yumeFriend-updater.exe：主程序退出后替换目录并重启（D456）。

由 PyInstaller 以 onefile 方式构建，随桌面 zip 分发。只依赖 Python 标准库。
"""

from __future__ import annotations

import argparse
import ctypes
import json
import os
import shutil
import subprocess
import sys
import time
import traceback
from pathlib import Path


def _log(path: Path, message: str) -> None:
    line = f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {message}"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")
    except OSError:
        pass


def _append_history(
    path: Path | None,
    *,
    event: str,
    version: str,
    from_version: str,
    message: str = "",
) -> None:
    if path is None:
        return
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        item = {
            "at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "event": event,
            "version": version,
            "from_version": from_version,
            "message": message,
        }
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(item, ensure_ascii=False) + "\n")
    except OSError:
        pass


def _wait_for_process(pid: int, timeout: float = 120.0) -> None:
    if pid <= 0:
        return
    kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
    synchronize = 0x00100000
    handle = kernel32.OpenProcess(synchronize, False, int(pid))
    if not handle:
        return
    try:
        kernel32.WaitForSingleObject(handle, int(timeout * 1000))
    finally:
        kernel32.CloseHandle(handle)


def _remove(path: Path) -> None:
    if path.exists():
        shutil.rmtree(path, ignore_errors=True)


def _relaunch(exe: Path, cwd: Path) -> subprocess.Popen:
    return subprocess.Popen([str(exe)], cwd=str(cwd), close_fds=True)


def apply_update(
    *,
    pid: int,
    install_dir: str | Path,
    staging_dir: str | Path,
    relaunch_path: str | Path,
    log_file: str | Path,
    history_file: str | Path | None = None,
    version: str = "",
    from_version: str = "",
    settle_seconds: float = 12.0,
) -> int:
    log_path = Path(log_file)
    install = Path(install_dir).resolve()
    staging = Path(staging_dir).resolve()
    relaunch = Path(relaunch_path).resolve()
    backup = install.parent / f"{install.name}.old-{int(time.time())}"
    user_data_tmp = install.parent / f".yf-user-data-{int(time.time())}"
    history_path = Path(history_file) if history_file else None

    _log(log_path, f"updater start pid={pid} install={install} staging={staging}")
    _wait_for_process(pid)
    _log(log_path, "main process exited")

    try:
        if not (staging / "yumeFriend.exe").is_file():
            raise RuntimeError("staging 里没有 yumeFriend.exe")
        if not install.is_dir():
            raise RuntimeError(f"安装目录不存在：{install}")

        user_data = install / "user_data"
        if user_data.exists():
            if user_data_tmp.exists():
                _remove(user_data_tmp)
            user_data.rename(user_data_tmp)
            _log(log_path, "user_data moved aside")

        if backup.exists():
            _remove(backup)
        install.rename(backup)
        _log(log_path, f"old install moved to {backup}")
        staging.rename(install)
        _log(log_path, "new install moved into place")

        if user_data_tmp.exists():
            user_data_tmp.rename(install / "user_data")
            _log(log_path, "user_data restored")

        proc = _relaunch(relaunch, install)
        _log(log_path, f"new app relaunched pid={proc.pid}")
        time.sleep(settle_seconds)
        if proc.poll() is None:
            _log(log_path, "new app still running; update complete")
            _append_history(
                history_path,
                event="applied",
                version=version,
                from_version=from_version,
            )
            _remove(backup)
            return 0
        raise RuntimeError(f"新版本启动后退出，code={proc.poll()}")
    except Exception as exc:  # noqa: BLE001
        _log(log_path, "update failed:\n" + traceback.format_exc())
        try:
            # 如果新目录已经换进去，先挪走
            failed = None
            if install.exists() and backup.exists():
                failed = install.parent / f".yf-failed-{int(time.time())}"
                install.rename(failed)
            if backup.exists():
                backup.rename(install)
            if user_data_tmp.exists():
                user_data_tmp.rename(install / "user_data")
            elif failed is not None and (failed / "user_data").exists():
                (failed / "user_data").rename(install / "user_data")
            _log(log_path, "rolled back to old install")
            _relaunch(install / "yumeFriend.exe", install)
        except Exception:  # noqa: BLE001
            _log(log_path, "rollback failed:\n" + traceback.format_exc())
        _log(log_path, f"update error: {exc}")
        _append_history(
            history_path,
            event="failed",
            version=version,
            from_version=from_version,
            message=str(exc),
        )
        return 1


def main() -> int:
    if len(sys.argv) <= 1:
        try:
            ctypes.windll.user32.MessageBoxW(  # type: ignore[attr-defined]
                0,
                "这是 yumeFriend 的自动更新器。\n\n"
                "请从 yumeFriend 的「设置 → 更新」里点「立即更新」；"
                "直接双击不会做任何事。",
                "yumeFriend 更新器",
                0x40,
            )
        except Exception:  # noqa: BLE001
            pass
        return 0
    parser = argparse.ArgumentParser()
    parser.add_argument("--pid", type=int, required=True)
    parser.add_argument("--install-dir", required=True)
    parser.add_argument("--staging-dir", required=True)
    parser.add_argument("--relaunch", required=True)
    parser.add_argument("--log", required=True)
    parser.add_argument("--history", default="")
    parser.add_argument("--version", default="")
    parser.add_argument("--from-version", default="")
    args = parser.parse_args()
    return apply_update(
        pid=args.pid,
        install_dir=args.install_dir,
        staging_dir=args.staging_dir,
        relaunch_path=args.relaunch,
        log_file=args.log,
        history_file=args.history or None,
        version=args.version,
        from_version=args.from_version,
    )


if __name__ == "__main__":
    sys.exit(main())
