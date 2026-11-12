"""桌面外壳共用的那几个 Win32 小动作（D70 的弹窗 + D119 的桌宠都用）。

为什么单独一个文件：这些是和"DPI 换算 / 焦点 / 任务栏按钮"打交道的**踩过坑的代码**，
两个窗口都要用一遍。复制两份的下场是——修了一处、另一处还是老的。

坐标系的坑（实测，见 D70）：pywebview 的 `move()` 用**逻辑（CSS）像素**，
而 `GetWindowRect()` / `GetMonitorInfo()` 用**物理像素**，150% 缩放的机器上差 1.5 倍。
我们只把"同一套坐标系里量出来的两个值"相减来求比例，所以下面这些函数
（除了 `move_window_physical`）**一律不假设坐标系**，只保证彼此一致。
"""

from __future__ import annotations

import ctypes
import time
from ctypes import wintypes
from typing import Any

from loguru import logger

GWL_EXSTYLE = -20
WS_EX_TOOLWINDOW = 0x00000080
DWMWA_WINDOW_CORNER_PREFERENCE = 33
DWMWA_USE_IMMERSIVE_DARK_MODE = 20
DWMWCP_ROUND = 2

SW_HIDE = 0
SW_SHOWNOACTIVATE = 4
SW_SHOW = 5
SW_RESTORE = 9

WS_EX_LAYERED = 0x00080000
LWA_COLORKEY = 0x00000001

SWP_NOSIZE = 0x0001
SWP_NOMOVE = 0x0002
SWP_NOZORDER = 0x0004
SWP_NOACTIVATE = 0x0010
SWP_FRAMECHANGED = 0x0020


class _MonitorInfo(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("rcMonitor", wintypes.RECT),
        ("rcWork", wintypes.RECT),
        ("dwFlags", wintypes.DWORD),
    ]


def user32() -> Any:
    return ctypes.windll.user32


def window_rect(hwnd: int) -> tuple[int, int, int, int]:
    """窗口矩形（物理像素）：(left, top, right, bottom)。"""
    rect = wintypes.RECT()
    user32().GetWindowRect(hwnd, ctypes.byref(rect))
    return rect.left, rect.top, rect.right, rect.bottom


def work_area_of(hwnd: int) -> tuple[int, int, int, int]:
    """窗口所在显示器的可用区域（**不含任务栏**）。

    用 `MonitorFromWindow` 而不是 `SystemParametersInfo`：后者在"进程是不是 DPI 感知"
    不同状态下的返回值不一样（实测踩过），而 `GetMonitorInfo` 和 `GetWindowRect`
    **一定在同一个坐标系**里，我们只关心两者的差。
    """
    monitor = user32().MonitorFromWindow(hwnd, 2)  # MONITOR_DEFAULTTONEAREST
    return _work_area(monitor)


def work_area_at_point(x: int, y: int) -> tuple[int, int, int, int]:
    """(x, y) 所在的显示器可用区域（物理像素）—— 桌宠在别的屏时气泡要跟过去。"""
    monitor = user32().MonitorFromPoint(
        wintypes.POINT(int(x), int(y)), 2  # MONITOR_DEFAULTTONEAREST
    )
    return _work_area(monitor)


def _work_area(monitor: int) -> tuple[int, int, int, int]:
    info = _MonitorInfo()
    info.cbSize = ctypes.sizeof(_MonitorInfo)
    if not user32().GetMonitorInfoW(monitor, ctypes.byref(info)):
        return 0, 0, 1920, 1080
    work = info.rcWork
    return work.left, work.top, work.right, work.bottom


def round_corners(hwnd: int) -> None:
    """让 Windows 11 给这个窗口做**原生圆角**（Win10 上调不通就忽略，那边显示直角）。"""
    try:
        pref = ctypes.c_int(DWMWCP_ROUND)
        ctypes.windll.dwmapi.DwmSetWindowAttribute(
            hwnd,
            DWMWA_WINDOW_CORNER_PREFERENCE,
            ctypes.byref(pref),
            ctypes.sizeof(pref),
        )
    except Exception:  # noqa: BLE001
        logger.debug("DWM 圆角不可用（Win10 或旧版本），按直角显示")


def set_dark_titlebar(hwnd: int, dark: bool = True) -> bool:
    """把窗口的**标题栏/边框**变成深色（Win10 1809+ 的 DWM 属性，D131）。

    应用是深色主题、标题栏却是白的，看着像"两个软件拼一起"——所以主题切换时前端会喊一声。
    """
    if not hwnd:
        return False
    try:
        value = ctypes.c_int(1 if dark else 0)
        ctypes.windll.dwmapi.DwmSetWindowAttribute(
            hwnd, DWMWA_USE_IMMERSIVE_DARK_MODE, ctypes.byref(value), ctypes.sizeof(value)
        )
        return True
    except Exception:  # noqa: BLE001
        logger.debug("深色标题栏不可用（老系统），忽略")
        return False


def hide_window(hwnd: int) -> None:
    user32().ShowWindow(hwnd, SW_HIDE)


def show_no_activate(hwnd: int) -> None:
    """露出来但**不抢焦点**（`window.show()` 会抢，正在打字时很讨厌）。"""
    user32().ShowWindow(hwnd, SW_SHOWNOACTIVATE)


def drop_taskbar_button(hwnd: int) -> None:
    """摘掉任务栏按钮（无边框窗口默认还是会占一个）。"""
    ex = user32().GetWindowLongW(hwnd, GWL_EXSTYLE)
    user32().SetWindowLongW(hwnd, GWL_EXSTYLE, ex | WS_EX_TOOLWINDOW)
    user32().SetWindowPos(
        hwnd, 0, 0, 0, 0, 0,
        SWP_NOMOVE | SWP_NOSIZE | SWP_NOZORDER | SWP_NOACTIVATE | SWP_FRAMECHANGED,
    )


def is_visible(hwnd: int) -> bool:
    return bool(hwnd) and bool(user32().IsWindowVisible(hwnd))


def bring_to_front(hwnd: int) -> None:
    """还原（如果最小化了）+ 拉到最前 + 抢焦点（D213：桌宠「回到主界面」用）。

    单靠 pywebview 的 `window.show()` 对**已经最小化**的窗口无效（WinForms 的
    `Show()` 不会改 WindowState）——用户看到的就是"点了没反应"。
    `SetForegroundWindow` 在 Windows 上可能被系统拒绝（前台锁），失败也不报错，
    所以是"能抢到就抢，抢不到至少已经还原 + 显示了"。

    ⚠️ **只在 `IsIconic` 时才 SW_RESTORE**：无脑 restore 会把"最大化"的窗口打回普通大小
    （pywebview 的 `window.restore()` 就是无脑设 Normal，所以这里不用它）。
    """
    if not hwnd:
        return
    u = user32()
    if u.IsIconic(hwnd):
        u.ShowWindow(hwnd, SW_RESTORE)
    else:
        u.ShowWindow(hwnd, SW_SHOW)
    try:
        u.SetForegroundWindow(hwnd)
    except Exception:  # noqa: BLE001
        logger.debug("SetForegroundWindow 被系统拒绝（忽略，窗口已还原）")


def find_window(title: str) -> int:
    hwnd = user32().FindWindowW(None, title)
    return int(hwnd) if hwnd else 0


def wait_hwnd(title: str, timeout: float = 3.0) -> int:
    """等窗口句柄出现（pywebview 建窗口是异步的，刚 create 完还拿不到）。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        hwnd = find_window(title)
        if hwnd:
            return hwnd
        time.sleep(0.05)
    return 0


def move_window_physical(hwnd: int, x: int, y: int) -> None:
    """把窗口挪到物理像素 (x, y)（气泡用它对齐桌宠的物理坐标）。"""
    user32().SetWindowPos(
        hwnd, 0, int(x), int(y), 0, 0, SWP_NOSIZE | SWP_NOZORDER | SWP_NOACTIVATE
    )

