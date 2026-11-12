"""桌面外壳：**关窗 ≠ 退出**（D16 / architecture.md §9）。

启动顺序：

    拿单实例锁（拿不到 → 提示"已经在跑了"并退出）
      ↓
    后台线程跑 uvicorn（127.0.0.1:8001，同时把前端页面发出来；D283 固定端口）
      ↓
    等 /api/health 通
      ↓
    开 pywebview 窗口 + pystray 托盘

窗口的"关闭按钮"被改成**隐藏**；要真退出得点托盘菜单的「退出」。

用法：
    python -m backend.desktop.app                 # 正常启动
    python -m backend.desktop.app --headless      # 只跑后端（调试 / 无 GUI 环境）
"""

from __future__ import annotations

import argparse
import contextlib
import ctypes
import json
import os
import sys
import threading
import time
import webbrowser

import httpx
import uvicorn
from loguru import logger

from backend import notify
from backend.config import HOST, PORT, RESOURCE_DIR, USER_DATA_DIR
from backend.desktop.pet import PetWindow
from backend.desktop.popup import PopupNotifier
from backend.desktop import win32
from backend.desktop.single_instance import SingleInstance
from backend.main import configure_logging
from backend.services import speech_service

# v2（D91）：端口被别的程序占了就自动换一个；挑中的端口写在这里，脚本靠它找到我们
RUNTIME_PORT_FILE = USER_DATA_DIR / "runtime_port.txt"
_main_window = None
_tray_icon = None
_update_exit_requested = False


def _port_is_free(port: int) -> bool:
    """这个端口现在能不能**独占**绑定。

    ⚠️ 千万别设 `SO_REUSEADDR`：Windows 上它会让我们"绑成功"但收不到请求 ——
    这正是 2026-09-19 那次"程序打不开"的元凶（8000 被别的程序占着，我们照样绑上了，
    桌面外壳的健康检查 30 秒连不通，于是自己退出）。
    """
    import socket

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        try:
            sock.bind((HOST, port))
            return True
        except OSError:
            return False


def choose_port(
    preferred: int = PORT, *, tries: int = 20, fallback: bool = True
) -> tuple[int, str | None] | None:
    """挑一个能用的端口：优先 `preferred`；`fallback=False` 时被占就返回 None。

    D283：桌面版默认 **固定 8001**（`fallback=False`），不再自动往后换，
    否则手机端书签/PWA 的端口会漂移。`fallback=True` 只留给测试/兼容。
    """
    if _port_is_free(preferred):
        return preferred, None
    if not fallback:
        return None
    for candidate in range(preferred + 1, preferred + tries + 1):
        if _port_is_free(candidate):
            return candidate, (
                f"端口 {preferred} 被别的程序占着（yumeFriend 平时用它），"
                f"这次自动改用 {candidate}"
            )
    return None


def _write_runtime_port(port: int) -> None:
    """把实际端口落盘 —— `scripts\\stop_backend.ps1` 靠它找到我们（不然它会去杀占着
    8001 的那个**别的程序**）。"""
    try:
        RUNTIME_PORT_FILE.parent.mkdir(parents=True, exist_ok=True)
        RUNTIME_PORT_FILE.write_text(str(port), encoding="utf-8")
    except OSError as exc:  # noqa: PERF203 —— 写不了也不该拦住启动
        logger.warning("写运行端口文件失败（不影响使用）：{}", exc)


def _clear_runtime_port() -> None:
    try:
        RUNTIME_PORT_FILE.unlink(missing_ok=True)
    except OSError:
        pass


def _start_backend(port: int = PORT) -> uvicorn.Server:
    """后端跑在**后台线程**里（和 GUI 同一个进程 —— 托盘退出能一起收干净）。"""
    # ⚠️ 这里**不能**用字符串 "backend.app:app"：打包成 exe 后 uvicorn 的字符串导入
    # 会报 `Could not import module "backend.app"`（那个 import 在静态分析里看不见，
    # 模块没被收进包，实测踩过）。显式 import 拿到对象传进去，开发/打包都一样。
    from backend.app import app as asgi_app

    config = uvicorn.Config(asgi_app, host=HOST, port=port, log_level="info")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, name="yumefriend-backend", daemon=True)
    thread.start()
    return server


def _wait_for_backend(base_url: str, timeout: float = 30.0) -> bool:
    """等后端就绪。**还要认一下身份**：端口上可能蹲着别的程序（它也可能回 200）。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            response = httpx.get(f"{base_url}/api/health", timeout=2.0, trust_env=False)
            if response.status_code == 200 and response.json().get("app") == "yumeFriend":
                return True
        except Exception:  # noqa: BLE001 —— 还没起来很正常
            pass
        time.sleep(0.3)
    return False


def _tray_image():
    """托盘图标 = 同一个 app 图标（D127：logo 一处画、四处用）。"""
    from backend.desktop.icon import app_icon

    return app_icon(64)

def _message_box(text: str, title: str = "yumeFriend") -> None:
    try:
        ctypes.windll.user32.MessageBoxW(0, text, title, 0x40)  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001
        print(f"{title}: {text}")


def install_popup(window, *, anchor=None) -> PopupNotifier:  # noqa: ANN001
    """把"桌面弹窗"挂到主窗口上（D70）。

    抽成函数是为了**能单独验证这条链路**（不用起整个桌面外壳）：
    只要给一个 pywebview 窗口，它就会在页面加载完成后开始接收通知。

    `anchor`（D119）：返回桌宠窗口的物理矩形；给了它，气泡就弹在桌宠正上方，
    桌宠没开/返回 None 时回退到右下角。
    """
    def _current_theme() -> str:
        """弹窗要跟主窗口当前主题一致（夜里弹一张白卡片很刺眼）。"""
        try:
            return str(window.evaluate_js("document.documentElement.dataset.theme") or "light")
        except Exception:  # noqa: BLE001 —— 问不到就按浅色
            return "light"

    def _open_friend(friend_id: str) -> None:
        """点了弹窗 → 把主窗口叫出来，并跳到那个朋友。"""
        try:
            window.show()
        except Exception:  # noqa: BLE001
            logger.opt(exception=True).warning("唤起主窗口失败")
        try:
            window.evaluate_js(
                f"window.yumeOpenFriend && window.yumeOpenFriend({json.dumps(friend_id)})"
            )
        except Exception:  # noqa: BLE001
            logger.opt(exception=True).warning("跳转到朋友失败（窗口已经打开了）")

    popup = PopupNotifier(
        main_title="yumeFriend",
        on_click=_open_friend,
        theme=_current_theme,
        anchor=anchor,
    )

    def _show_popup(event: dict) -> None:
        try:
            popup.show(
                friend_id=str(event.get("friend_id") or ""),
                friend_name=str(event.get("friend_name") or ""),
                text=str(event.get("text") or ""),
            )
        except Exception:  # noqa: BLE001
            logger.opt(exception=True).warning("弹窗失败（已忽略）")

    def _on_notify(event: dict) -> None:
        # 弹窗要挪窗口、等句柄，会花几百毫秒；**别卡住后端的事件循环**，丢给线程
        threading.Thread(target=_show_popup, args=(event,), daemon=True).start()

    def _on_loaded() -> None:
        # 等主窗口页面加载完再挂钩子：这时 GUI 一定起来了
        #（pywebview 建窗口是异步的，早挂钩子会拿不到窗口句柄）
        notify.register(_on_notify)
        logger.info("桌面弹窗已就绪（朋友主动找你 / 提醒到点时会弹卡片，D70）")

    window.events.loaded += _on_loaded
    return popup


def request_exit_for_update() -> None:
    """D456：更新器已经启动，让桌面外壳正常退出，交给更新器替换文件。"""
    global _update_exit_requested
    _update_exit_requested = True
    threading.Thread(
        target=lambda: shutdown(_tray_icon, _main_window),
        name="yumefriend-update-exit",
        daemon=True,
    ).start()


def _run_window_and_tray(base_url: str) -> None:
    global _main_window, _tray_icon
    import pystray
    import webview

    # 桌宠（D119）：窗口本体在 backend/desktop/pet.py，里面画什么在 frontend/src/PetApp.tsx。
    # 下面这两个函数先定义、后用到 `window`（闭包在调用时才取变量，所以顺序没关系）。
    def _current_theme() -> str:
        """桌宠要跟主窗口当前主题一致（夜里弹一张白卡片很刺眼）。"""
        try:
            return str(window.evaluate_js("document.documentElement.dataset.theme") or "light")
        except Exception:  # noqa: BLE001 —— 问不到就按浅色
            return "light"

    def _show_main(friend_id: str | None = None) -> None:
        """把主窗口叫出来；给了 friend_id 就跳过去（桌宠的「回到主界面」）。

        2026-09-27（D213）：用户报"主界面按钮没用"。原来只调 `window.show()` —— 窗口
        处于**最小化**时 pywebview/WinForms 的 `Show()` 不会还原（还是最小化），看着就是
        "点了没反应"。现在先 `restore()`，再 show，最后用 Win32 兜一次"拉到最前 + 还原"。
        """
        logger.debug("桌宠：回到主界面 friend={}", friend_id)
        try:
            window.show()
        except Exception:  # noqa: BLE001
            logger.opt(exception=True).warning("唤起主窗口失败")
        try:
            hwnd = win32.wait_hwnd("yumeFriend", 0.5)
            if hwnd:
                # 最小化 → 还原；否则只"拉到最前"（**不动最大化状态**）
                win32.bring_to_front(hwnd)
        except Exception:  # noqa: BLE001
            logger.debug("把主窗口拉到最前失败（忽略）")
        if not friend_id:
            return
        try:
            window.evaluate_js(
                f"window.yumeOpenFriend && window.yumeOpenFriend({json.dumps(friend_id)})"
            )
        except Exception:  # noqa: BLE001
            logger.opt(exception=True).warning("跳转到朋友失败（窗口已经打开了）")

    def _set_window_theme(theme: str) -> None:
        """标题栏跟着主题走（D131）：深色主题就把标题栏/边框也变深。"""
        win32.set_dark_titlebar(win32.wait_hwnd("yumeFriend", 1.0), theme == "dark")

    def _toggle_fullscreen() -> None:
        try:
            window.toggle_fullscreen()
        except Exception:  # noqa: BLE001
            logger.opt(exception=True).warning("切全屏失败（已忽略）")

    def _open_game(friend_id: str | None = None) -> None:
        """桌宠的「游戏管理」：把主窗口叫出来并切到全屏游戏管理页。"""
        _show_main(friend_id)
        try:
            window.evaluate_js(
                f"window.yumeOpenGame && window.yumeOpenGame({json.dumps(friend_id)})"
            )
        except Exception:  # noqa: BLE001
            logger.opt(exception=True).warning("打开游戏管理失败（窗口已经打开了）")

    pet = PetWindow(
        base_url,
        on_open_main=_show_main,
        on_open_game=_open_game,
        on_toggle_fullscreen=_toggle_fullscreen,
        on_set_theme=_set_window_theme,
    )

    window = webview.create_window(
        "yumeFriend",
        # ?shell=1 是给前端看的标记：跑在桌面外壳里 → 用我们自己的弹窗（D70），
        # 不再去要系统通知权限（两套一起弹会重复，用户明确不要系统通知）
        f"{base_url}/?shell=1",
        width=1120,
        height=780,
        min_size=(860, 580),
        # 深色（D131）：窗口底用我们深色主题的底色，启动那一下不闪白
        background_color="#17140f",
        # 主窗口也能喊"开桌宠"（D119）
        js_api=pet.api,
    )
    _main_window = window
    # 「保存图片」要弹原生「另存为」对话框（D163）→ 把主窗口交给它
    pet.api.set_window(window)

    def _on_closing() -> bool:
        # 关窗 ≠ 退出（D16）：藏起来，进程继续跑（朋友还要主动找你）
        window.hide()
        return False

    window.events.closing += _on_closing
    # 气泡跟着桌宠走：桌宠开着就弹它正上方，没开就回退右下角（D119 第 6 条）
    install_popup(window, anchor=pet.rect_if_visible)

    def _quit(icon, _item) -> None:  # noqa: ANN001
        shutdown(icon, window)

    def _show(icon, _item) -> None:  # noqa: ANN001
        window.show()

    def _toggle_pet(icon, _item) -> None:  # noqa: ANN001
        from backend.features import flags as feature_flags  # noqa: PLC0415

        if not feature_flags.enabled("pet"):
            return
        if pet.is_visible:
            pet.hide()
        else:
            pet.show(pet.friend_id)

    def _quit_pet(icon, _item) -> None:  # noqa: ANN001
        pet.close()

    tray = pystray.Icon(
        "yumeFriend",
        _tray_image(),
        "yumeFriend",
        menu=pystray.Menu(
            pystray.MenuItem("打开窗口", _show, default=True),
            pystray.MenuItem("桌宠（显示 / 躲起来）", _toggle_pet),
            pystray.MenuItem("退出桌宠模式", _quit_pet),
            pystray.MenuItem("退出", _quit),
        ),
    )
    tray.run_detached()  # Windows 上要脱离主线程（主线程留给 pywebview）
    _tray_icon = tray
    logger.info("托盘已就绪（关窗不会退出，点托盘「退出」才真的退）")
    # 窗口图标（= 标题栏左上角 + 任务栏按钮）：跟界面/托盘**同一张**（D132）
    # pywebview 的 winforms 后端认这个参数（会 self.Icon = Icon(path)）
    icon_path = RESOURCE_DIR / "assets" / "yumeFriend.ico"
    try:
        webview.start(icon=str(icon_path) if icon_path.is_file() else None)
    except Exception as exc:  # noqa: BLE001 —— 没装 WebView2 / 窗口起不来（T3.0-A4）
        _fallback_to_browser(base_url, exc)


# 装 WebView2 的官方下载页（Q17 的兜底要指路，别只说"起不来"）
WEBVIEW2_DOWNLOAD = "https://go.microsoft.com/fwlink/p/?LinkId=2124703"


def _fallback_to_browser(base_url: str, exc: Exception) -> None:
    """窗口起不来 → 说人话 + 用系统浏览器打开，后端继续跑（v3.0 / T3.0-A4）。

    为什么不让它退出：后端 + 托盘都还在，用户在浏览器里照样能聊天；
    要退走托盘「退出」。**绝不静默** —— 日志和弹窗都得写清是"没装 WebView2"。
    """
    logger.exception("窗口起不来（多半是没装 WebView2）：{}", exc)
    _message_box(
        "yumeFriend 的窗口起不来（多半是这台电脑没装 WebView2 运行时）。\n\n"
        "点「确定」帮 TA 用系统浏览器打开 —— 聊天 / 语音这些照常能用。\n\n"
        f"想装 WebView2（推荐）：{WEBVIEW2_DOWNLOAD}"
    )
    try:
        webbrowser.open(base_url)
    except Exception:  # noqa: BLE001 —— 连浏览器都打不开也不能把进程带崩
        logger.warning("连浏览器都没打开（地址：{}）", base_url)
    while True:  # 后端还活着，让用户从浏览器用；退出走托盘
        time.sleep(1)


def shutdown(icon=None, window=None) -> None:  # noqa: ANN001
    """托盘「退出」的收尾：**必须真的把进程结束掉**。

    为什么不能只 `icon.stop()` + `window.destroy()`：
    **实测那样做完，进程会变成"僵尸"** —— 窗口没了、托盘图标也没了，
    但 python 还在后台跑，于是单实例锁和 8001 端口都被占着，
    再启动就永远提示"已经打开了"（而且用户根本找不到它去关）。

    uvicorn / pywebview / pystray 都可能留下非守护线程，靠函数返回退不干净，
    所以这里显式 `os._exit`。先把日志刷一下，别把最后几行丢了。
    """
    logger.info("退出 yumeFriend")
    _clear_runtime_port()
    # v2.1（T2.1-2 / D96）：**我们起的语音服务进程也要收掉**。
    # 必须显式做：下面 `os._exit(0)` 是硬退，FastAPI 的 lifespan 收尾**根本不会跑**
    #（实测漏过一个 STT 孤儿进程，占着 9881 —— 和 D67 那个僵尸是同一类坑）
    with contextlib.suppress(Exception):
        speech_service.stop_all_ours()
    with contextlib.suppress(Exception):
        if icon is not None:
            icon.stop()
    with contextlib.suppress(Exception):
        if window is not None:
            window.destroy()
    time.sleep(0.3)  # 给日志队列一点时间落盘
    os._exit(0)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="yumeFriend 桌面版")
    parser.add_argument("--headless", action="store_true", help="只跑后端，不开窗口/托盘")
    args = parser.parse_args(argv)

    configure_logging()
    lock = SingleInstance()
    if not lock.acquire():
        logger.warning("已经有一个 yumeFriend 在跑（单实例锁生效）")
        _message_box(
            "yumeFriend 已经在运行了。\n\n"
            "先看右下角托盘里有没有它的图标，有就点「打开窗口」。\n"
            "如果托盘里也找不到（上次可能没退干净），"
            "跑一次 scripts\\stop_backend.ps1 再启动。"
        )
        return 0

    picked = choose_port(fallback=False)
    if picked is None:
        logger.error("固定端口 {} 被别的程序占着，起不来", PORT)
        _message_box(
            f"yumeFriend 起不来：固定端口 {PORT} 被别的程序占着。\n\n"
            "先关掉占着这个端口的程序再试；或者跑一次 scripts\\stop_backend.ps1 清一下。"
        )
        lock.release()
        return 1
    port, note = picked
    base_url = f"http://{HOST}:{port}"
    _write_runtime_port(port)
    if note:
        logger.warning("{}", note)
    server = _start_backend(port)
    if not _wait_for_backend(base_url):
        logger.error("后端 30 秒内没起来（{}），退出", base_url)
        _message_box(
            f"yumeFriend 的后端起不来（{base_url}）。\n\n"
            "看 user_data\\logs\\yumefriend.log 里的报错，或者窗口左下角的「日志」。"
        )
        lock.release()
        return 1
    logger.info("后端就绪：{}", base_url)

    try:
        if args.headless:
            logger.info("--headless：只跑后端（Ctrl+C 退出）")
            while True:
                time.sleep(1)
        else:
            _run_window_and_tray(base_url)
    except KeyboardInterrupt:  # noqa: PERF203
        pass
    finally:
        server.should_exit = True
        lock.release()
        logger.info("已退出")
    return 0


if __name__ == "__main__":
    sys.exit(main())
