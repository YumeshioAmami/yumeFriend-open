"""原生桌宠窗口（D121）：真透明的分层窗口 + 一个原生输入小窗。

**为什么不用 WebView**：WebView2 的画面是独立合成的一层，窗口级的"色键 / 形状裁剪 /
系统毛玻璃"都碰不到它（D120 末尾三种失败实测）——所以"只有立绘"只能自己画。

结构（都在这个文件里，`pet_render.py` 负责画）：
  · 分层窗口 `UpdateLayeredWindow` 画立绘 + 她的话 + 按钮 + 菜单；**alpha=0 的地方系统直接放过去**
    （连鼠标也一起穿）。
  · 一个**独立的小窗口 + 原生 EDIT** 当输入框：分层窗口里放不了子控件、也收不了中文输入法，
    独立小窗这两件事都是现成的（输入法/光标/选择）。
  · 数据全走后端 HTTP：朋友列表 / 历史（content、moods、audio_segments 都是现成的）/
    发消息（SSE 在后台线程读完，完成后重拉历史）；语音用 winsound 顺序播。
    整套跑在自己的线程 + 消息循环里，跟 pywebview 那个线程互不干扰。
"""

from __future__ import annotations

import ctypes
import json
import math
import os
import queue
import random
import threading
import time
import winsound
from ctypes import wintypes
from typing import Any, Callable

import httpx
import numpy as np
from loguru import logger
from PIL import Image, ImageDraw

from backend.config import AVATAR_DIR, PORTRAIT_DIR, RESOURCE_DIR, USER_DATA_DIR
from backend.desktop import pet_render as R
from backend.desktop import win32
from backend.desktop.live2d_renderer import Live2DRenderer
from backend.services import live2d_settings_service

# ── 常量 ────────────────────────────────────────────────────────────
PET_CLASS = "yumeFriendPetLayered"
INPUT_CLASS = "yumeFriendPetInput"
PET_TITLE = "yumeFriendPet"
EDIT_ID = 1001

WM_APP = 0x8000
WM_PET_SYNC = WM_APP + 1
WM_MOUSELEAVE = 0x02A3
WM_TIMER = 0x0113
WM_QUIT = 0x0012
WM_LBUTTONDOWN = 0x0201
WM_LBUTTONUP = 0x0202
WM_MOUSEMOVE = 0x0200
WM_RBUTTONUP = 0x0205
WM_DESTROY = 0x0002
WM_ERASEBKGND = 0x0014
WM_CTLCOLOREDIT = 0x0133
WM_SETFONT = 0x0030
WM_KEYDOWN = 0x0100
WM_NCHITTEST = 0x0084
PM_REMOVE = 0x0001
QS_ALLINPUT = 0x04FF
HTTRANSPARENT = -1  # 命中测试：这块让鼠标穿过去（气泡不该在点击范围里）

POLL_MS = 2000
# 她正在回复时，历史里的 assistant 正文会每秒落一次库；桌宠跟着快一点轮询，
# 气泡才不会"整段停住，等回复完才突然出现"。
STREAM_POLL_MS = 400
REVEAL_MS = 30
REVEAL_CHARS = 1
# 悬停按钮的宽容期（秒）：鼠标在边界抖一下别让按钮闪（D130）
HOVER_GRACE_SECONDS = 0.35
# 悬停检测的间隔。**为什么不靠 WM_MOUSEMOVE**：这台机器上分层窗口收不到鼠标移动消息
# （按住拖动那种有 SetCapture 的能收到）—— 实测 `_moves` 一直是 0。定时看光标最稳。
HOVER_MS = 120
# 立绘动作的推帧间隔（D123；2026-11-08 用户要求高帧）。配合
# 高精度消息循环的目标帧率；实际受渲染/GDI 合成速度限制。
MOTION_TARGET_FPS = 120
# 没语音时，一段停多久自动接下一段（D124）
AUTO_ADVANCE_IDLE_SECONDS = 3.5
# 她说完之后，那个框再停留多久就收起来（D124 补充）
BUBBLE_IDLE_SECONDS = 6.0
# D465：说完话后过几秒回到「正常表情」；之后没人理她时，随机做几个
# **纯视觉**的小表情（不写心情、不进历史、不播情绪音效）。
EXPRESSION_RELAX_SECONDS = 5.0
IDLE_EXPRESSION_MIN_SECONDS = 10.0
IDLE_EXPRESSION_MAX_SECONDS = 24.0
IDLE_EXPRESSION_HOLD_MIN_SECONDS = 4.0
IDLE_EXPRESSION_HOLD_MAX_SECONDS = 9.0
# 这些标签太像“真的在难过/生气”，空闲随机表情里先避开；用户明确想要
# 某个模型的全量随机时再放开这个过滤。
IDLE_EXPRESSION_EXCLUDE = (
    "生气",
    "愤怒",
    "厌恶",
    "害怕",
    "慌张",
    "伤心",
    "哭",
    "发情",
    "色气",
    "羞耻",
)

SCALES: tuple[float, ...] = (0.8, 1.0, 1.25, 1.5)
DEFAULT_SCALE = 1.0
MARGIN = 28
# 图标按钮「放大 / 缩小」一步的倍率（2026-09-27；范围仍走 clamp_scale）
ZOOM_STEP = 1.1

STATE_FILE = USER_DATA_DIR / "pet_window.json"

WS_POPUP = 0x80000000
WS_CHILD = 0x40000000
WS_VISIBLE = 0x10000000
ES_LEFT = 0x0000
ES_AUTOHSCROLL = 0x0080
WS_EX_LAYERED = 0x00080000
WS_EX_TOOLWINDOW = 0x00000080
WS_EX_NOACTIVATE = 0x08000000
HWND_TOPMOST = ctypes.c_void_p(-1)
SW_HIDE = 0
SW_SHOWNOACTIVATE = 4
SWP_NOSIZE = 0x0001
SWP_NOMOVE = 0x0002
SWP_NOZORDER = 0x0004
SWP_NOACTIVATE = 0x0010
SWP_SHOWWINDOW = 0x0040
ULW_ALPHA = 0x00000002
AC_SRC_OVER = 0x00
AC_SRC_ALPHA = 0x01
SRCCOPY = 0x00CC0020
GWLP_WNDPROC = -4
VK_RETURN = 0x0D
VK_ESCAPE = 0x1B

user32 = ctypes.WinDLL("user32", use_last_error=True)
gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
try:
    winmm = ctypes.WinDLL("winmm")
except OSError:  # pragma: no cover - Windows 桌面一定有
    winmm = None

LRESULT = ctypes.c_ssize_t

class WNDCLASSEXW(ctypes.Structure):
    _fields_ = [
        ("cbSize", ctypes.c_uint),
        ("style", ctypes.c_uint),
        ("lpfnWndProc", ctypes.c_void_p),
        ("cbClsExtra", ctypes.c_int),
        ("cbWndExtra", ctypes.c_int),
        ("hInstance", wintypes.HINSTANCE),
        ("hIcon", wintypes.HICON),
        ("hCursor", wintypes.HANDLE),
        ("hbrBackground", wintypes.HBRUSH),
        ("lpszMenuName", wintypes.LPCWSTR),
        ("lpszClassName", wintypes.LPCWSTR),
        ("hIconSm", wintypes.HICON),
    ]


class POINT(ctypes.Structure):
    _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]


class SIZE(ctypes.Structure):
    _fields_ = [("cx", ctypes.c_long), ("cy", ctypes.c_long)]


class BLENDFUNCTION(ctypes.Structure):
    _fields_ = [
        ("BlendOp", ctypes.c_ubyte),
        ("BlendFlags", ctypes.c_ubyte),
        ("SourceConstantAlpha", ctypes.c_ubyte),
        ("AlphaFormat", ctypes.c_ubyte),
    ]


class UPDATELAYEREDWINDOWINFO(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("hdcDst", wintypes.HDC),
        ("pptDst", ctypes.POINTER(POINT)),
        ("psize", ctypes.POINTER(SIZE)),
        ("hdcSrc", wintypes.HDC),
        ("pptSrc", ctypes.POINTER(POINT)),
        ("crKey", wintypes.COLORREF),
        ("pblend", ctypes.POINTER(BLENDFUNCTION)),
        ("dwFlags", wintypes.DWORD),
        ("prcDirty", ctypes.POINTER(wintypes.RECT)),
    ]


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [
        ("biSize", ctypes.c_uint),
        ("biWidth", ctypes.c_long),
        ("biHeight", ctypes.c_long),
        ("biPlanes", ctypes.c_ushort),
        ("biBitCount", ctypes.c_ushort),
        ("biCompression", ctypes.c_uint),
        ("biSizeImage", ctypes.c_uint),
        ("biXPelsPerMeter", ctypes.c_long),
        ("biYPelsPerMeter", ctypes.c_long),
        ("biClrUsed", ctypes.c_uint),
        ("biClrImportant", ctypes.c_uint),
    ]


class BITMAPINFO(ctypes.Structure):
    _fields_ = [("bmiHeader", BITMAPINFOHEADER), ("bmiColors", ctypes.c_uint * 3)]


class TRACKMOUSEEVENT(ctypes.Structure):
    _fields_ = [
        ("cbSize", ctypes.c_uint),
        ("dwFlags", ctypes.c_uint),
        ("hwndTrack", wintypes.HWND),
        ("dwHoverTime", ctypes.c_uint),
    ]


WNDPROC = ctypes.WINFUNCTYPE(
    LRESULT, wintypes.HWND, ctypes.c_uint, wintypes.WPARAM, wintypes.LPARAM
)

# ⚠️ 64 位下句柄/指针必须声明清楚，否则会被截成 32 位（真机实测：CreateWindowExW 直接
# OverflowError）。下面这些 argtypes 都是被这个坑逼出来的。
HWND = wintypes.HWND
HDC = ctypes.c_void_p
HGDIOBJ = ctypes.c_void_p

user32.CreateWindowExW.argtypes = [
    wintypes.DWORD,
    wintypes.LPCWSTR,
    wintypes.LPCWSTR,
    wintypes.DWORD,
    ctypes.c_int,
    ctypes.c_int,
    ctypes.c_int,
    ctypes.c_int,
    HWND,
    wintypes.HMENU,
    wintypes.HINSTANCE,
    wintypes.LPVOID,
]
user32.SetWindowPos.argtypes = [
    HWND,
    HWND,
    ctypes.c_int,
    ctypes.c_int,
    ctypes.c_int,
    ctypes.c_int,
    ctypes.c_uint,
]
user32.ShowWindow.argtypes = [HWND, ctypes.c_int]
user32.GetDC.argtypes = [HWND]
user32.GetDC.restype = HDC
user32.ReleaseDC.argtypes = [HWND, HDC]
user32.UpdateLayeredWindow.argtypes = [
    HWND,
    HDC,
    ctypes.POINTER(POINT),
    ctypes.POINTER(SIZE),
    HDC,
    ctypes.POINTER(POINT),
    wintypes.COLORREF,
    ctypes.POINTER(BLENDFUNCTION),
    wintypes.DWORD,
]
user32.SendMessageW.argtypes = [HWND, ctypes.c_uint, wintypes.WPARAM, wintypes.LPARAM]
user32.SendMessageW.restype = LRESULT
user32.GetWindowTextW.argtypes = [HWND, wintypes.LPWSTR, ctypes.c_int]
user32.SetWindowTextW.argtypes = [HWND, wintypes.LPCWSTR]
user32.GetCursorPos.argtypes = [ctypes.POINTER(POINT)]
user32.GetPhysicalCursorPos.argtypes = [ctypes.POINTER(POINT)]
user32.WindowFromPoint.argtypes = [POINT]
user32.WindowFromPoint.restype = HWND
user32.GetDpiForWindow.argtypes = [HWND]
user32.GetDpiForWindow.restype = ctypes.c_uint
user32.GetDpiForWindow.argtypes = [HWND]
user32.GetDpiForWindow.restype = ctypes.c_uint
user32.TrackMouseEvent.argtypes = [ctypes.POINTER(TRACKMOUSEEVENT)]
user32.GetForegroundWindow.restype = HWND
user32.GetWindowThreadProcessId.argtypes = [HWND, ctypes.POINTER(wintypes.DWORD)]
user32.GetWindowThreadProcessId.restype = wintypes.DWORD
user32.SetTimer.argtypes = [HWND, ctypes.c_size_t, ctypes.c_uint, ctypes.c_void_p]
user32.SetTimer.restype = ctypes.c_void_p
user32.PostMessageW.argtypes = [HWND, ctypes.c_uint, wintypes.WPARAM, wintypes.LPARAM]
user32.GetMessageW.argtypes = [
    ctypes.POINTER(wintypes.MSG),
    HWND,
    ctypes.c_uint,
    ctypes.c_uint,
]
user32.RegisterClassExW.argtypes = [ctypes.POINTER(WNDCLASSEXW)]
user32.RegisterClassExW.restype = ctypes.c_ushort
user32.LoadCursorW.argtypes = [wintypes.HINSTANCE, ctypes.c_void_p]
user32.LoadCursorW.restype = ctypes.c_void_p
user32.FillRect.argtypes = [HDC, ctypes.POINTER(wintypes.RECT), wintypes.HBRUSH]
gdi32.SetTextColor.argtypes = [HDC, wintypes.COLORREF]
gdi32.SetTextColor.restype = wintypes.COLORREF
gdi32.SetBkColor.argtypes = [HDC, wintypes.COLORREF]
gdi32.SetBkColor.restype = wintypes.COLORREF
user32.GetClientRect.argtypes = [HWND, ctypes.POINTER(wintypes.RECT)]
user32.DestroyWindow.argtypes = [HWND]
user32.SetCapture.argtypes = [HWND]
user32.SetCapture.restype = HWND
user32.ReleaseCapture.argtypes = []
gdi32.CreateCompatibleDC.argtypes = [HDC]
gdi32.CreateCompatibleDC.restype = HDC
gdi32.SelectObject.argtypes = [HDC, HGDIOBJ]
gdi32.SelectObject.restype = HGDIOBJ
gdi32.DeleteObject.argtypes = [HGDIOBJ]
gdi32.DeleteDC.argtypes = [HDC]
gdi32.CreateSolidBrush.argtypes = [wintypes.COLORREF]
gdi32.CreateSolidBrush.restype = HGDIOBJ
gdi32.CreateFontW.restype = HGDIOBJ
gdi32.CreateDIBSection.argtypes = [
    HDC,
    ctypes.c_void_p,
    ctypes.c_uint,
    ctypes.POINTER(ctypes.c_void_p),
    wintypes.HANDLE,
    ctypes.c_uint,
]


user32.CreateWindowExW.restype = wintypes.HWND
user32.DefWindowProcW.restype = LRESULT
user32.DefWindowProcW.argtypes = [
    wintypes.HWND,
    ctypes.c_uint,
    wintypes.WPARAM,
    wintypes.LPARAM,
]
user32.CallWindowProcW.restype = LRESULT
user32.CallWindowProcW.argtypes = [
    ctypes.c_void_p,
    HWND,
    ctypes.c_uint,
    wintypes.WPARAM,
    wintypes.LPARAM,
]
user32.SetWindowLongPtrW.restype = ctypes.c_void_p
user32.SetWindowLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_void_p]
user32.GetWindowLongPtrW.restype = ctypes.c_void_p
user32.GetWindowLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int]
user32.UpdateLayeredWindow.restype = ctypes.c_bool
kernel32.GetModuleHandleW.restype = wintypes.HMODULE
gdi32.CreateDIBSection.restype = wintypes.HBITMAP


# ── 小工具（纯函数，好测）───────────────────────────────────────────
def premultiplied_bgra(frame: Image.Image) -> bytes:
    """PIL 的 RGBA → 分层窗口要的**预乘 BGRA**。

    Pillow 自带预乘 alpha 的 ``RGBa`` 模式，转换在 C 里做，比 split +
    ImageChops.multiply + merge 快约 1/3；在 608×1596 的桌宠窗口上实测
    7.2ms → 5.0ms，每帧都能省一点（D428 已取消窗口尺寸上限，算法不变）。
    """
    if frame.mode == "RGBa":
        return frame.tobytes("raw", "BGRa")
    return frame.convert("RGBa").tobytes("raw", "BGRa")


def clamp_scale(value: Any) -> float:
    """自由缩放（D122）：夹在 `MIN_SCALE`~`MAX_SCALE`，中间的值都收。"""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return DEFAULT_SCALE
    return max(R.MIN_SCALE, min(R.MAX_SCALE, number))


def scaled_size(scale: float) -> tuple[int, int]:
    return R.window_size(scale)


def resized_scale(scale0: float, distance: float, dist0: float) -> float:
    """自由缩放（D122）的算式：按住抓角拖时"离窗口左上角的距离"按比例换算成缩放。

    抽成纯函数是为了能单测（真机上那套坐标单位太乱了，见 `_tick_hover` 的注释）。
    """
    return clamp_scale(scale0 * distance / max(1.0, dist0))


def offscreen_delta(
    rect: tuple[int, int, int, int], work: tuple[int, int, int, int]
) -> tuple[int, int]:
    """要挪多少（物理像素）才能让 `rect` 回到工作区里；已经在里面就是 (0, 0)。"""
    left, top, right, bottom = rect
    work_left, work_top, work_right, work_bottom = work
    dx = 0
    if left < work_left:
        dx = work_left - left
    elif right > work_right:
        dx = work_right - right
    dy = 0
    if top < work_top:
        dy = work_top - top
    elif bottom > work_bottom:
        dy = work_bottom - bottom
    return dx, dy


def load_state(path=STATE_FILE) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_state(state: dict[str, Any], path=STATE_FILE) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
    except OSError as exc:
        logger.warning("桌宠位置没存上（不影响使用）：{}", exc)


# ── 后端数据（HTTP）─────────────────────────────────────────────────
class PetData:
    """桌宠要的那点数据。**不重写聊天逻辑**：后端给什么读什么。"""

    def __init__(self, base_url: str) -> None:
        self.base_url = base_url.rstrip("/")
        self._client = httpx.Client(base_url=self.base_url, timeout=15.0, trust_env=False)

    def friends(self) -> list[dict[str, Any]]:
        response = self._client.get("/api/friends")
        response.raise_for_status()
        return list(response.json().get("friends") or [])

    def history(self, friend_id: str, limit: int = 20) -> list[dict[str, Any]]:
        response = self._client.get(
            f"/api/friends/{friend_id}/messages", params={"limit": limit}
        )
        response.raise_for_status()
        return list(response.json().get("messages") or [])

    def send(self, friend_id: str, text: str) -> None:
        """发一条：SSE 在后台读完（正文不解析），完成后由调用方重拉历史。"""
        with self._client.stream(
            "POST",
            f"/api/friends/{friend_id}/messages",
            json={"content": text},
            timeout=120.0,
        ) as response:
            response.raise_for_status()
            for _line in response.iter_lines():
                pass

    def mark_read(self, friend_id: str) -> None:
        self._client.post(f"/api/friends/{friend_id}/read")

    def set_focus(self, friend_id: str | None) -> None:
        self._client.post("/api/focus", json={"friend_id": friend_id})

    def audio(self, url: str) -> bytes | None:
        try:
            response = self._client.get(url, timeout=30.0)
            response.raise_for_status()
            return response.content
        except Exception:  # noqa: BLE001
            return None

    def close(self) -> None:
        self._client.close()


def play_wavs(chunks: list[bytes]) -> None:
    """顺序播（winsound 是同步的，放在自己的线程里）。"""
    for chunk in chunks:
        if not chunk:
            continue
        try:
            winsound.PlaySound(chunk, winsound.SND_MEMORY | winsound.SND_NODEFAULT)
        except Exception:  # noqa: BLE001
            logger.debug("桌宠播音失败（跳过这一段）")


# ── 桌宠窗口 ────────────────────────────────────────────────────────
class PetNativeWindow:
    """桌宠：分层窗口 + 输入小窗。公开接口跟老的 `PetWindow` 一致。"""

    def __init__(
        self,
        base_url: str,
        *,
        on_open_main: Callable[[str | None], None] | None = None,
        on_open_game: Callable[[str | None], None] | None = None,
        state_path=STATE_FILE,
    ) -> None:
        self._base_url = base_url
        self._on_open_main = on_open_main
        self._on_open_game = on_open_game
        self._state_path = state_path
        self._data = PetData(base_url)

        state = load_state(state_path)
        self._scale = clamp_scale(state.get("scale", DEFAULT_SCALE))
        self._locked = bool(state.get("locked", False))
        self._ui_scale = 1.0
        self._friend_id: str | None = state.get("friend_id") or None
        self._live2d_model_id: str | None = None
        self._pos: tuple[int, int] | None = (
            (int(state["x"]), int(state["y"])) if "x" in state and "y" in state else None
        )

        self._ready = threading.Event()
        self._thread: threading.Thread | None = None
        self._hwnd = 0
        self._input_hwnd = 0
        self._edit_hwnd = 0
        self._commands: queue.Queue[tuple[Callable[[], Any], threading.Event, dict]] = (
            queue.Queue()
        )
        self._proc = WNDPROC(self._wndproc)
        self._input_proc = WNDPROC(self._input_wndproc)
        self._edit_proc = WNDPROC(self._edit_wndproc)
        self._prev_edit_proc: int = 0
        self._brush = 0
        self._font = 0
        self._font_height = 0

        self._friends: list[dict[str, Any]] = []
        self._paragraphs: list[str] = []
        self._moods: list[str] = []
        self._index = 0
        # D465：真实消息 mood（_moods / 气泡）和“画在脸上的表情”分开。
        # 空闲随机表情只是视觉层，绝不回写 _moods / 历史 / 心情。
        self._idle_visual_mood = ""
        self._idle_visual_until = 0.0
        self._next_idle_visual_at = 0.0
        self._visual_relax_until = 0.0
        self._visual_was_speaking = False
        self._idle_label_key: tuple[str | None, str | None] | None = None
        self._idle_label_cache: list[str] = []
        self._busy = False
        self._mute = False
        self._hovered = False  # 鼠标在桌宠范围里（决定"两个框出不出来"）
        self._buttons_hover = False  # 鼠标在立绘上（决定悬停按钮）
        self._input_shown = False
        # 点过「说话」图标 → 输入框保持显示（直到失焦且没字 / 发出去 / ESC）2026-09-27
        self._input_wanted = False
        self._menu = False
        self._layout: R.Layout | None = None
        self._layout_key: tuple[Any, ...] | None = None
        # 2026-11-07：桌宠重做的性能路径。
        # · UI 覆盖层按状态缓存，不用每帧重新排字/画图标；
        # · Live2D 直接用预乘 alpha 贴到常驻 DIB，空闲帧不再整窗合成 + 全部预乘。
        self._chrome_image: Image.Image | None = None
        self._chrome_key: tuple[Any, ...] | None = None
        self._fast_chrome_array: np.ndarray | None = None
        self._fast_chrome_base_array: np.ndarray | None = None
        self._fast_chrome_base_key: tuple[Any, ...] | None = None
        self._fast_chrome_key: tuple[Any, ...] | None = None
        self._fast_patches: list[R.Rect] = []
        self._fast_chrome_dirty: list[R.Rect] = []
        # 气泡单独一层：流式文字每 30ms 变一次，只重画这块小图，
        # 不能再把整窗 UI 底板 copy / diff 一遍（大桌宠下会造成明显掉帧）。
        self._fast_bubble_key: tuple[Any, ...] | None = None
        self._fast_bubble_array: np.ndarray | None = None
        self._fast_bubble_origin: tuple[int, int] = (0, 0)
        self._fast_bubble_rect: tuple[int, int, int, int] | None = None
        self._fast_bubble_dirty: tuple[int, int, int, int] | None = None
        self._bubble_lines_key: tuple[Any, ...] | None = None
        self._bubble_lines_cache: list[str] = []
        self._fast_base_ready = False
        self._fast_last_box: tuple[int, int, int, int] | None = None
        self._gdi_screen_dc = 0
        self._gdi_memory_dc = 0
        self._gdi_bitmap = 0
        self._gdi_old_bitmap = 0
        self._gdi_bits: Any = None
        self._gdi_buffer: Any = None
        self._gdi_array: np.ndarray | None = None
        self._gdi_size = (0, 0)
        self._char_dc = 0
        self._char_bitmap = 0
        self._char_old_bitmap = 0
        self._char_bits: Any = None
        self._char_buffer: Any = None
        self._char_array: np.ndarray | None = None
        self._char_size = (0, 0)
        self._portrait: Image.Image | None = None
        self._portrait_key: tuple[str, str] | None = None
        # D424：桌宠优先用 Live2D；模型缺失/坏了就自动回退静态立绘。
        self._live2d = Live2DRenderer()
        # 动作变换（缩放/旋转）结果缓存（2026-09-27，D213）
        self._transform_cache = R.TransformCache()
        self._visible = False
        self._rect: tuple[int, int, int, int] | None = None
        self._drag: dict[str, Any] | None = None
        self._last_message_id: str | None = None
        self._played: set[str] = set()
        # 语音（D134）：**一条队列 + 唯一一个播放线程**，一段接一段；每段播完通知翻页
        self._spoken: dict[str, set[int]] = {}
        # v3.14（D182）：被主窗口"认领"过的消息 —— 主窗口在前台时由页面出声，
        # 桌宠就别插嘴（窗口后来又切走了也不能补播，不然跟她叠着说）
        self._page_owned: set[str] = set()
        self._voice_queue: queue.Queue[tuple[str, int, str]] = queue.Queue()
        self._voice_thread: threading.Thread | None = None
        self._reveal_left = 0
        self._speech_until = 0.0
        self._focus_tick = 0
        self._sync_tick = 0
        self._syncing = False
        self._poll_ms = POLL_MS
        # 立绘动作（D123）：换表情时播一次，33ms 一帧推着走
        self._motion_name: str | None = None
        self._motion_start = 0.0
        self._motion_mood: str | None = None
        # 自动模式（D124）：桌宠默认一直往下说，不用点
        self._auto = True
        self._auto_deadline = 0.0
        self._history_messages: list[dict[str, Any]] = []
        # 她的话：说出来先显示，说完静一会儿就收起来（D124 补充；悬停时会临时再显示）
        self._bubble_shown = False
        self._bubble_hide_at = 0.0
        # value = (缩放好的图, 是不是"头像占位")
        self._portrait_scaled: dict[tuple[Any, int, int], tuple[Image.Image, bool]] = {}
        # 2026-09-27（用户："角色情绪转变的时候，换动作表情会特别卡顿特别慢"）：
        # 真正贵的是"换情绪时在 UI 线程上现解一张 1536×2048 的图 + resize"。
        # ① 按 (朋友, 情绪) 缓存**解码好的**立绘（换回去瞬时命中）；
        # ② 后台把这位朋友**所有情绪**先解码 + 按当前尺寸缩放好（预热）。
        self._portrait_cache: dict[tuple[str, str], Image.Image] = {}
        self._portrait_lock = threading.Lock()
        self._prefetching = False
        self._prefetch_at = 0.0
        self._portrait_is_avatar = False
        # "光标坐标 → 物理坐标"的倍率：**用一次真实的鼠标移动标定**（见 `_calibrate_cursor`）。
        # 之前按 DPI 猜，结果在真机里猜反了（pywebview 跑起来时进程是 DPI 感知的，
        # 光标和窗口矩形本来就在同一套坐标里）—— 表现就是"悬停看不见框、只有移动时闪一下"。
        self._cursor_ratio: float | None = None
        self._hover_grace_until = 0.0
        self._last_paint = 0.0
        self._paint_pending = False
        self._edit_size: tuple[int, int] = (0, 0)

    # ── 对外（跟老 PetWindow 一样）──────────────────────────────────
    @property
    def is_open(self) -> bool:
        return bool(self._hwnd)

    @property
    def is_visible(self) -> bool:
        return self._visible

    @property
    def friend_id(self) -> str | None:
        return self._friend_id

    @property
    def scale(self) -> float:
        return self._scale

    def show(self, friend_id: str | None = None) -> bool:
        self._ensure_thread()
        friend = friend_id or self._friend_id
        return bool(self._call(lambda: self._show_on_thread(friend)))

    def hide(self) -> None:
        self._call(lambda: self._hide_on_thread())

    def close(self) -> None:
        self._call(lambda: self._close_on_thread())

    def set_scale(self, value: Any) -> float:
        result = self._call(lambda: self._set_scale_on_thread(value))
        return float(result if result is not None else self._scale)

    def rect_if_visible(self) -> tuple[int, int, int, int] | None:
        return self._rect if self._visible else None

    def apply_rects(self, _rects: Any = None) -> bool:  # 兼容旧接口（现在不需要了）
        return True

    # ── 线程与命令 ──────────────────────────────────────────────────
    def _ensure_thread(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._ready.clear()
        self._thread = threading.Thread(target=self._run, name="yumefriend-pet", daemon=True)
        self._thread.start()
        self._ready.wait(10.0)

    def _call(self, fn: Callable[[], Any], timeout: float = 8.0) -> Any:
        if not self._hwnd or not self._thread or not self._thread.is_alive():
            return None
        done = threading.Event()
        box: dict[str, Any] = {}
        self._commands.put((fn, done, box))
        user32.PostMessageW(self._hwnd, WM_PET_SYNC, 0, 0)
        if not done.wait(timeout):
            logger.warning("桌宠命令超时")
            return None
        return box.get("result")

    def _post(self, fn: Callable[[], Any]) -> None:
        if self._hwnd:
            self._commands.put((fn, threading.Event(), {}))
            user32.PostMessageW(self._hwnd, WM_PET_SYNC, 0, 0)

    # ── 线程：建窗口 + 消息循环 ─────────────────────────────────────
    def _run(self) -> None:
        high_res_timer = False
        try:
            if winmm is not None:
                try:
                    winmm.timeBeginPeriod(1)
                    high_res_timer = True
                except Exception:  # noqa: BLE001
                    pass
            self._register_classes()
            self._create_windows()
            self._place()
            self._ready.set()
            self._poll_ms = POLL_MS
            user32.SetTimer(self._hwnd, 1, self._poll_ms, None)
            user32.SetTimer(self._hwnd, 2, REVEAL_MS, None)
            user32.SetTimer(self._hwnd, 3, HOVER_MS, None)
            self._sync_data(initial=True)
            message = wintypes.MSG()
            # SetTimer 的最小粒度约 10~15ms，想稳过 60fps 不能靠它。
            # 这里自己跑一个高精度消息循环：空闲时最多等 2ms，到点就画一帧。
            target_interval = 1.0 / MOTION_TARGET_FPS
            next_motion = time.perf_counter()
            running = True
            while running:
                while user32.PeekMessageW(
                    ctypes.byref(message), 0, 0, 0, PM_REMOVE
                ):
                    if message.message == WM_QUIT:
                        running = False
                        break
                    user32.TranslateMessage(ctypes.byref(message))
                    user32.DispatchMessageW(ctypes.byref(message))
                if not running:
                    break
                now = time.perf_counter()
                # 不要用 1ms 的 MsgWait 去补最后一点帧间隔：Windows 11 会把
                # 这种短超时合并到 ~15.6ms，导致 120fps 实际掉成 60~70fps。
                # 先按高精度计时器 sleep，最多 3ms；真正到点后直接画下一帧。
                if now >= next_motion:
                    self._tick_motion()
                    now = time.perf_counter()
                    # 按绝对节拍推进；如果这一帧画得比 8.3ms 慢，就立刻接上
                    # 下一帧（最多补 4 帧，防止系统休眠后疯狂追帧）。
                    if now - next_motion > target_interval * 4:
                        next_motion = now + target_interval
                    else:
                        next_motion += target_interval
                    continue
                remaining = next_motion - now
                if remaining > 0.0005:
                    time.sleep(min(0.003, remaining))
        except Exception:  # noqa: BLE001
            logger.opt(exception=True).warning("桌宠线程挂了")
        finally:
            if high_res_timer and winmm is not None:
                try:
                    winmm.timeEndPeriod(1)
                except Exception:  # noqa: BLE001
                    pass
            self._ready.set()

    def _register_classes(self) -> None:
        instance = kernel32.GetModuleHandleW(None)
        cursor = user32.LoadCursorW(None, ctypes.c_void_p(32512))  # IDC_ARROW
        for name, proc in ((PET_CLASS, self._proc), (INPUT_CLASS, self._input_proc)):
            window_class = WNDCLASSEXW()
            window_class.cbSize = ctypes.sizeof(WNDCLASSEXW)
            window_class.lpfnWndProc = ctypes.cast(proc, ctypes.c_void_p)
            window_class.hInstance = instance
            window_class.hCursor = cursor
            window_class.lpszClassName = name
            if not user32.RegisterClassExW(ctypes.byref(window_class)):
                logger.debug("窗口类 {} 已经注册过了", name)

    def _create_windows(self) -> None:
        instance = kernel32.GetModuleHandleW(None)
        width, height = scaled_size(self._scale)
        self._hwnd = user32.CreateWindowExW(
            WS_EX_LAYERED | WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE,
            PET_CLASS,
            PET_TITLE,
            WS_POPUP,
            0,
            0,
            width,
            height,
            0,
            0,
            instance,
            0,
        )
        try:
            dpi = float(user32.GetDpiForWindow(self._hwnd) or 96)
            self._ui_scale = max(1.0, min(2.0, dpi / 96.0))
        except Exception:  # noqa: BLE001
            self._ui_scale = 1.0
        self._input_hwnd = user32.CreateWindowExW(
            WS_EX_TOOLWINDOW,
            INPUT_CLASS,
            PET_TITLE + "Input",
            WS_POPUP,
            0,
            0,
            width,
            R.INPUT_HEIGHT,
            0,
            0,
            instance,
            0,
        )
        self._brush = gdi32.CreateSolidBrush(0x00201A16)
        self._edit_hwnd = user32.CreateWindowExW(
            0,
            "EDIT",
            "",
            WS_CHILD | WS_VISIBLE | ES_LEFT | ES_AUTOHSCROLL,
            12,
            7,
            width - 24,
            R.INPUT_HEIGHT - 14,
            self._input_hwnd,
            EDIT_ID,
            instance,
            0,
        )
        # 字号**跟着缩放走**（2026-09-27，用户："输入框太小、光标小得跟没有一样"）。
        # 以前写死 -14：放大到 2.5× 时框有一百多像素高、字还是 14px，光标自然像根线。
        self._apply_edit_font()
        self._prev_edit_proc = int(user32.GetWindowLongPtrW(self._edit_hwnd, GWLP_WNDPROC))
        user32.SetWindowLongPtrW(
            self._edit_hwnd, GWLP_WNDPROC, ctypes.cast(self._edit_proc, ctypes.c_void_p)
        )
        win32.round_corners(self._input_hwnd)
        user32.ShowWindow(self._input_hwnd, SW_HIDE)
        user32.ShowWindow(self._hwnd, SW_HIDE)

    # ── 窗口过程 ────────────────────────────────────────────────────
    def _wndproc(self, hwnd, msg, wparam, lparam):  # noqa: ANN001
        try:
            if msg == WM_PET_SYNC:
                self._drain_commands()
                return 0
            if msg == WM_TIMER:
                if wparam == 1:
                    self._sync_data()
                elif wparam == 2:
                    self._tick_reveal()
                elif wparam == 3:
                    self._tick_hover()
                return 0
            if msg == WM_LBUTTONDOWN:
                self._on_press(self._xy(lparam))
                return 0
            if msg == WM_MOUSEMOVE:
                self._on_move(self._xy(lparam))
                return 0
            if msg == WM_LBUTTONUP:
                self._on_release(self._xy(lparam))
                return 0
            if msg == WM_RBUTTONUP:
                if self._locked:
                    return 0
                self._menu = not self._menu
                self._request_repaint()
                return 0
            if msg == WM_NCHITTEST:
                # 气泡那条带（立绘上方）→ 鼠标穿过去（用户 2026-09-27："聊天框不该在点击范围里"）
                if self._transparent_at(lparam):
                    return HTTRANSPARENT
                return user32.DefWindowProcW(hwnd, msg, wparam, lparam)
            if msg == WM_MOUSELEAVE:
                # 2026-10-03（用户实测"按钮挪到左边就点不到了"）：**别在这里硬清**。
                # 按钮那一列在立绘实体**外面**，鼠标从立绘走到按钮要跨一段"透明区"——
                # 一离开实体就 WM_MOUSELEAVE，硬清 = 按钮立刻消失；而透明区收不到鼠标消息，
                # 没人把它救回来 → 看着就是"移上去就消失、点不到"。
                # 改成走统一的重新判定（`_tick_hover` 用全局光标 + chrome_hit + 350ms 宽限）。
                self._tick_hover()
                return 0
            if msg == WM_DESTROY:
                user32.PostQuitMessage(0)
                return 0
        except Exception:  # noqa: BLE001
            logger.opt(exception=True).debug("桌宠窗口过程出错（已忽略）")
        return user32.DefWindowProcW(hwnd, msg, wparam, lparam)

    def _input_wndproc(self, hwnd, msg, wparam, lparam):  # noqa: ANN001
        if msg == WM_ERASEBKGND:
            rect = wintypes.RECT()
            user32.GetClientRect(hwnd, ctypes.byref(rect))
            user32.FillRect(wparam, ctypes.byref(rect), self._brush)
            return 1
        if msg == WM_CTLCOLOREDIT:
            gdi32.SetTextColor(wparam, 0x00F0EDE8)
            gdi32.SetBkColor(wparam, 0x00201A16)
            return self._brush
        return user32.DefWindowProcW(hwnd, msg, wparam, lparam)

    def _edit_wndproc(self, hwnd, msg, wparam, lparam):  # noqa: ANN001
        if msg == WM_KEYDOWN and wparam == VK_RETURN:
            self._submit_input()
            return 0
        if msg == WM_KEYDOWN and wparam == VK_ESCAPE:
            user32.SetWindowTextW(hwnd, "")
            self._input_wanted = False
            try:
                user32.SetFocus(self._hwnd)
            except Exception:  # noqa: BLE001 —— 抢不回焦点就算了，失焦时会自己收
                pass
            self._sync_input_visibility()
            return 0
        return user32.CallWindowProcW(
            ctypes.c_void_p(self._prev_edit_proc), hwnd, msg, wparam, lparam
        )

    # ── 命令执行（都在桌宠线程里）──────────────────────────────────
    def _drain_commands(self) -> None:
        while True:
            try:
                fn, done, box = self._commands.get_nowait()
            except queue.Empty:
                return
            try:
                box["result"] = fn()
            except Exception:  # noqa: BLE001
                logger.opt(exception=True).warning("桌宠命令失败（已忽略）")
            finally:
                done.set()

    def _show_on_thread(self, friend_id: str | None) -> bool:
        if not self._hwnd:
            return False
        if friend_id:
            self._friend_id = friend_id
        self._visible = True
        self._idle_visual_mood = ""
        self._next_idle_visual_at = 0.0
        self._place()
        user32.ShowWindow(self._hwnd, SW_SHOWNOACTIVATE)
        user32.SetWindowPos(self._hwnd, HWND_TOPMOST, 0, 0, 0, 0, SWP_NOMOVE | SWP_NOSIZE)
        self._input_shown = False
        self._request_repaint()
        self._sync_input_visibility()
        self._save_state()
        self._sync_data()
        self._prefetch_portraits()  # 起手就把她的立绘热起来（2026-09-27）
        return True

    def _hide_on_thread(self) -> None:
        self._remember_position()
        self._visible = False
        self._idle_visual_mood = ""
        self._next_idle_visual_at = 0.0
        self._rect = None
        user32.ShowWindow(self._input_hwnd, SW_HIDE)
        user32.ShowWindow(self._hwnd, SW_HIDE)
        self._save_state()

    def _close_on_thread(self) -> None:
        self._hide_on_thread()
        self._live2d.close()
        self._release_gdi()
        if self._hwnd:
            user32.DestroyWindow(self._hwnd)
        self._hwnd = 0

    def _set_scale_on_thread(self, value: Any, *, throttle: bool = True) -> float:
        if self._locked:
            return self._scale
        self._scale = clamp_scale(value)
        width, height = scaled_size(self._scale)
        if self._hwnd:
            user32.SetWindowPos(
                self._hwnd, 0, 0, 0, width, height,
                SWP_NOMOVE | SWP_NOZORDER | SWP_NOACTIVATE,
            )
        self._apply_edit_font()  # 字号跟着缩放（2026-09-27，D213）
        self._keep_on_screen()
        # 缩放拖动时鼠标消息很密，重画跟不上就会"顿" → 限到 ~25fps（松手时补最后一帧）
        now = time.monotonic()
        if not throttle or now - self._last_paint >= 0.04:
            self._last_paint = now
            self._request_repaint()
        else:
            self._paint_pending = True
        self._save_state()
        self._prefetch_portraits()  # 尺寸变了 → 按新尺寸重新预热（内部节流 5s）
        return self._scale

    # ── 位置 ────────────────────────────────────────────────────────
    def _place(self) -> None:
        if self._pos is None:
            self._pos = self._default_position()
        user32.SetWindowPos(
            self._hwnd,
            HWND_TOPMOST,
            int(self._pos[0]),
            int(self._pos[1]),
            0,
            0,
            SWP_NOSIZE | SWP_NOACTIVATE,
        )
        self._rect = win32.window_rect(self._hwnd)
        # 2026-11-07（用户："点桌宠没效果"）：取消尺寸上限后，旧位置/大倍率
        # 可能把窗口整个放到屏幕外。显示前先按当前工作区夹回来，否则窗口
        # 其实已经创建但人眼看不到。
        self._keep_on_screen()

    def _default_position(self) -> tuple[int, int]:
        _left, _top, right, bottom = win32.work_area_of(self._hwnd)
        width, height = scaled_size(self._scale)
        return right - width - MARGIN, bottom - height - MARGIN

    @staticmethod
    def _edit_font_height(scale: float) -> int:
        """输入框字号（物理像素）：跟基准字号一起缩放，最小 14 别缩成看不见。"""
        return max(14, int(round(R.FONT_SIZE * scale)))

    def _apply_edit_font(self) -> None:
        """按当前缩放重建输入框字体（2026-09-27，D213）。

        用户反馈"输入框太小、光标小得跟没有一样"——根因是字号写死 `-14`
        不跟缩放：窗口放大到 2.5×、输入框高一百多像素，字还是 14px。
        """
        if not self._edit_hwnd:
            return
        height = self._edit_font_height(self._ui_scale)
        if height == self._font_height:
            return
        font = gdi32.CreateFontW(
            -height, 0, 0, 0, 400, 0, 0, 0, 134, 0, 0, 0, 0, "Microsoft YaHei UI"
        )
        if not font:
            return
        old = self._font
        self._font = font
        self._font_height = height
        user32.SendMessageW(self._edit_hwnd, WM_SETFONT, font, 1)
        if old:
            gdi32.DeleteObject(old)

    def _sync_input_geometry(self) -> None:
        """输入小窗贴在桌宠底部那块留白里（跟着走、跟着缩放）。"""
        if not self._hwnd or not self._input_hwnd or self._layout is None:
            return
        rect = self._rect or win32.window_rect(self._hwnd)
        area = self._layout.input_rect
        user32.SetWindowPos(
            self._input_hwnd,
            0,
            rect[0] + area.left,
            rect[1] + area.top,
            area.width,
            area.height,
            SWP_NOZORDER | SWP_NOACTIVATE,
        )
        edit_size = (area.width - 24, area.height - 16)
        if edit_size != self._edit_size:
            self._edit_size = edit_size
            user32.SetWindowPos(
                self._edit_hwnd,
                0,
                12,
                8,
                max(20, edit_size[0]),
                max(14, edit_size[1]),
                SWP_NOZORDER | SWP_NOACTIVATE,
            )

    def _work_area_physical(self) -> tuple[int, int, int, int]:
        """工作区（= 屏幕减任务栏），和 `GetWindowRect` 同一套坐标。

        2026-11-08：这里以前又乘了一次 DPI，主窗口是 system-aware 时
        `GetMonitorInfo` / `GetWindowRect` 本来就在同一套坐标里，再乘一次会把
        工作区放大，导致“窗口其实在屏幕外、程序却以为它在屏内”。现在直接返回，
        两种坐标由 Win32 自己保证一致。
        """
        return win32.work_area_of(self._hwnd)

    def _keep_on_screen(self) -> None:
        if not self._hwnd:
            return
        rect = win32.window_rect(self._hwnd)
        work = self._work_area_physical()
        width = rect[2] - rect[0]
        height = rect[3] - rect[1]
        work_width = work[2] - work[0]
        work_height = work[3] - work[1]
        # 窗口比工作区还大时，不再追求“完整显示”（物理上做不到），
        # 至少把左上角固定在可见区域，保证角色/按钮能被看到。
        if width >= work_width:
            x = work[0]
        elif rect[0] < work[0]:
            x = work[0]
        elif rect[2] > work[2]:
            x = work[2] - width
        else:
            x = rect[0]
        if height >= work_height:
            y = work[1]
        elif rect[1] < work[1]:
            y = work[1]
        elif rect[3] > work[3]:
            y = work[3] - height
        else:
            y = rect[1]
        if (x, y) != (rect[0], rect[1]):
            user32.SetWindowPos(
                self._hwnd, 0, x, y, 0, 0, SWP_NOSIZE | SWP_NOZORDER | SWP_NOACTIVATE
            )
            self._pos = (x, y)
        self._rect = win32.window_rect(self._hwnd)
        self._layout_key = None

    def _remember_position(self) -> None:
        if self._hwnd:
            rect = win32.window_rect(self._hwnd)
            self._pos = (rect[0], rect[1])
            self._rect = rect

    def _save_state(self) -> None:
        state: dict[str, Any] = {"scale": self._scale, "locked": bool(self._locked)}
        if self._friend_id:
            state["friend_id"] = self._friend_id
        if self._pos is not None:
            state["x"], state["y"] = self._pos
        save_state(state, self._state_path)

    # ── 画面 ────────────────────────────────────────────────────────
    def _name(self) -> str:
        for friend in self._friends:
            if friend.get("id") == self._friend_id:
                return str(friend.get("name") or "她")
        return "她"

    def _mood(self) -> str:
        if 0 <= self._index < len(self._moods) and self._moods[self._index]:
            return str(self._moods[self._index])
        return ""

    def _idle_expression_labels(self) -> list[str]:
        """当前模型可用于「空闲随机小表情」的标签（只读设置，不碰消息）。"""
        key = (self._friend_id, self._live2d_model_id)
        if key == self._idle_label_key:
            return self._idle_label_cache
        labels: list[str] = []
        try:
            labels = live2d_settings_service.labels_for_friend(
                self._friend_id,
                self._live2d_model_id,
            )
        except Exception:  # noqa: BLE001 —— 读不到设置就当没有随机表情
            labels = []
        self._idle_label_cache = [str(item) for item in labels if str(item).strip()]
        self._idle_label_key = key
        return self._idle_label_cache

    def _pick_idle_visual_mood(self) -> str:
        labels = self._idle_expression_labels()
        candidates = [
            label
            for label in labels
            if label != "正常"
            and label != self._idle_visual_mood
            and label != self._mood()
            and not any(word in label for word in IDLE_EXPRESSION_EXCLUDE)
        ]
        if not candidates:
            candidates = [
                label
                for label in labels
                if label != "正常"
                and label != self._idle_visual_mood
                and label != self._mood()
            ]
        return random.choice(candidates) if candidates else ""

    def _visual_mood(self) -> str:
        """画在脸上的表情。

        - 说话 / 刚说完的几秒：跟真实消息情绪；
        - 之后：回「正常」；
        - 没人理她时：随机挑一个纯视觉小表情，持续几秒再回正常。
        **随机表情不写回 `_moods`、不影响气泡标签、不进历史。**
        """
        if not self._live2d.active:
            return self._mood()
        now = time.monotonic()
        speaking = self._reveal_left > 0 or now < self._speech_until
        if speaking:
            self._visual_was_speaking = True
            self._visual_relax_until = now + EXPRESSION_RELAX_SECONDS
            self._idle_visual_mood = ""
            self._next_idle_visual_at = 0.0
            return self._mood()
        if self._visual_was_speaking:
            self._visual_was_speaking = False
            # 说完后的这几秒保持真实情绪，之后才回正常表情。
            self._next_idle_visual_at = self._visual_relax_until + random.uniform(
                IDLE_EXPRESSION_MIN_SECONDS,
                IDLE_EXPRESSION_MAX_SECONDS,
            )
        if now < self._visual_relax_until:
            return self._mood()
        busy = (
            self._busy
            or self._input_shown
            or self._menu
            or self._drag is not None
            or self._hovered
        )
        if busy:
            # 有人理她就别自己换表情；等空闲后重新计时。
            self._idle_visual_mood = ""
            self._next_idle_visual_at = 0.0
            return "正常"
        if self._idle_visual_mood:
            if now < self._idle_visual_until:
                return self._idle_visual_mood
            self._idle_visual_mood = ""
            self._next_idle_visual_at = now + random.uniform(
                IDLE_EXPRESSION_MIN_SECONDS,
                IDLE_EXPRESSION_MAX_SECONDS,
            )
            return "正常"
        if self._next_idle_visual_at <= 0:
            self._next_idle_visual_at = now + random.uniform(
                IDLE_EXPRESSION_MIN_SECONDS,
                IDLE_EXPRESSION_MAX_SECONDS,
            )
        if now >= self._next_idle_visual_at:
            picked = self._pick_idle_visual_mood()
            if picked:
                self._idle_visual_mood = picked
                self._idle_visual_until = now + random.uniform(
                    IDLE_EXPRESSION_HOLD_MIN_SECONDS,
                    IDLE_EXPRESSION_HOLD_MAX_SECONDS,
                )
                self._next_idle_visual_at = 0.0
                return picked
            self._next_idle_visual_at = now + random.uniform(
                IDLE_EXPRESSION_MIN_SECONDS,
                IDLE_EXPRESSION_MAX_SECONDS,
            )
        return "正常"

    def _current_text(self) -> str:
        if not self._paragraphs:
            # D234：这是**系统**写的空态文案，不许暗示"她在等你"（不给用户亏欠感）
            return "（还没说话）"
        index = max(0, min(self._index, len(self._paragraphs) - 1))
        text = self._paragraphs[index]
        if index == len(self._paragraphs) - 1 and self._reveal_left > 0:
            return text[: max(0, len(text) - self._reveal_left)]
        return text

    def _arm_speech(self, text: str, message: dict[str, Any] | None = None, index: int | None = None) -> None:
        """按音频时长或字数给 Live2D 一段开口时间（D440）。"""
        duration_ms: float | None = None
        if message is not None and index is not None:
            segments = message.get("audio_segments") or []
            if 0 <= index < len(segments):
                try:
                    duration_ms = float(segments[index].get("duration_ms") or 0) or None
                except (TypeError, ValueError):
                    duration_ms = None
        if duration_ms is None:
            per_char = 160.0
            min_ms = 600.0
            max_ms = 12000.0
            try:
                _model, settings = live2d_settings_service.settings_for_friend(
                    self._friend_id,
                    self._live2d_model_id,
                )
                speech = (settings or {}).get("speech") or {}
                per_char = float(speech.get("text_ms_per_char", per_char))
                min_ms = float(speech.get("min_ms", min_ms))
                max_ms = float(speech.get("max_ms", max_ms))
            except Exception:  # noqa: BLE001 —— 设置读不到就用默认口型
                pass
            duration_ms = max(min_ms, min(max_ms, len(text or "") * per_char))
        self._speech_until = time.monotonic() + duration_ms / 1000.0
        # 她一开口就取消空闲随机表情；说完后由 _visual_mood 延迟回正常。
        self._visual_was_speaking = True
        self._visual_relax_until = time.monotonic() + EXPRESSION_RELAX_SECONDS
        self._idle_visual_mood = ""
        self._next_idle_visual_at = 0.0

    def _load_portrait(self) -> Image.Image | None:
        """立绘（按情绪取）；**没有立绘就用头像兜底**。

        为什么必须兜底：这个窗口是"**画出来的东西才点得到**"（alpha=0 的地方系统直接放过去）。
        朋友还没导立绘时如果什么都不画，她身上就一块空白 → 按钮出不来、也点不了
        （用户实测报的 bug）。头像就是最自然的占位。
        """
        key = (self._friend_id or "", self._mood() or "正常")
        if self._portrait_key == key:
            return self._portrait
        self._portrait_key = key
        with self._portrait_lock:
            cached = self._portrait_cache.get(key)
        if cached is not None:
            self._portrait = cached
            self._portrait_is_avatar = False
            return cached
        self._portrait = None
        self._portrait_is_avatar = False
        directory = PORTRAIT_DIR / (self._friend_id or "")
        if directory.is_dir():
            self._portrait = self._open_portrait_file(directory, key[1])
        if self._portrait is None:
            avatar = AVATAR_DIR / f"{self._friend_id or ''}.png"
            if not avatar.is_file():
                avatar = AVATAR_DIR / "default.png"
            if avatar.is_file():
                try:
                    self._portrait = self._rounded_avatar(Image.open(avatar).convert("RGBA"))
                    self._portrait_is_avatar = True
                except Exception:  # noqa: BLE001
                    self._portrait = None
        if self._portrait is None:
            # 连头像都没有 → 画一个"首字母色块"（跟前端 Avatar 的兜底一个路子）。
            # **必须画点什么**：这个窗口是"画出来的东西才点得到"，全空的话她身上没一块能点。
            self._portrait = self._initial_avatar(self._name())
            self._portrait_is_avatar = True
        if self._portrait is not None and not self._portrait_is_avatar:
            with self._portrait_lock:
                self._portrait_cache[key] = self._portrait
                # 全尺寸解码图一张就 12MB（1536×2048×4）→ 只留最近 3 张，别爆内存
                if len(self._portrait_cache) > 3:
                    for stale in list(self._portrait_cache)[: len(self._portrait_cache) - 3]:
                        self._portrait_cache.pop(stale, None)
        return self._portrait

    @staticmethod
    def _open_portrait_file(directory, mood: str) -> Image.Image | None:
        """按情绪找立绘（找不到这个情绪就退到随便一张）—— 跟 `_load_portrait` 同一个口径。"""
        try:
            paths = [
                path
                for path in sorted(directory.iterdir())
                if path.suffix.lower() in {".png", ".webp", ".jpg", ".jpeg"}
            ]
        except Exception:  # noqa: BLE001
            return None
        wanted = [path for path in paths if path.stem == mood]
        for path in wanted + paths:
            try:
                return Image.open(path).convert("RGBA")
            except Exception:  # noqa: BLE001
                continue
        return None

    def _prefetch_portraits(self) -> None:
        """后台把这位朋友**所有情绪**的立绘先解码 + 按当前尺寸缩放好（2026-09-27）。

        换表情时 UI 线程就能直接命中缓存，不再现解一张大图 → 不卡。
        """
        now = time.monotonic()
        if self._prefetching or now - self._prefetch_at < 5:
            return
        friend_id = self._friend_id
        layout = self._layout
        if not friend_id or layout is None:
            return
        area = layout.portrait
        if area.width <= 0 or area.height <= 0:
            return
        directory = PORTRAIT_DIR / friend_id
        if not directory.is_dir():
            return
        try:
            moods = [
                path.stem
                for path in directory.iterdir()
                if path.suffix.lower() in {".png", ".webp", ".jpg", ".jpeg"}
            ]
        except Exception:  # noqa: BLE001
            return
        if not moods:
            return
        self._prefetching = True
        self._prefetch_at = now

        def worker() -> None:
            try:
                for mood in moods:
                    key = (friend_id, mood)
                    fit_key = (key, area.width, area.height)
                    with self._portrait_lock:
                        if fit_key in self._portrait_scaled:
                            continue
                    image = self._open_portrait_file(directory, mood)
                    if image is None:
                        continue
                    ratio = min(area.width / image.width, area.height / image.height)
                    fitted = image.resize(
                        (max(1, int(image.width * ratio)), max(1, int(image.height * ratio))),
                        Image.BILINEAR,
                    )
                    # 只留**缩放好的小图**（~0.6MB/张）；全尺寸那张用完就扔（12MB/张，留多了爆内存）
                    with self._portrait_lock:
                        self._portrait_scaled[fit_key] = (fitted, False)
                        if len(self._portrait_scaled) > 24:
                            for stale in list(self._portrait_scaled)[: len(self._portrait_scaled) - 24]:
                                self._portrait_scaled.pop(stale, None)
            except Exception:  # noqa: BLE001
                logger.opt(exception=True).debug("立绘预热失败（忽略）")
            finally:
                self._prefetching = False

        threading.Thread(target=worker, daemon=True).start()

    @staticmethod
    def _initial_avatar(name: str) -> Image.Image:
        """没立绘也没头像时的占位：圆角色块 + 名字首字。"""
        size = 256
        palette = [
            (194, 105, 71),   # 伴侣橙
            (90, 120, 160),
            (120, 140, 100),
            (150, 110, 150),
            (170, 130, 90),
        ]
        color = palette[sum(ord(char) for char in (name or "?")) % len(palette)]
        image = Image.new("RGBA", (size, size), (0, 0, 0, 0))
        draw = ImageDraw.Draw(image)
        draw.rounded_rectangle((0, 0, size - 1, size - 1), radius=size // 8, fill=color + (255,))
        initial = (name or "?").strip()[:1].upper() or "?"
        try:
            font = R._font(int(size * 0.45), bold=True)
        except Exception:  # noqa: BLE001
            font = None
        if font is not None:
            box = draw.textbbox((0, 0), initial, font=font)
            draw.text(
                ((size - (box[2] - box[0])) / 2 - box[0], (size - (box[3] - box[1])) / 2 - box[1]),
                initial,
                font=font,
                fill=(255, 255, 255, 240),
            )
        return image

    @staticmethod
    def _rounded_avatar(image: Image.Image) -> Image.Image:
        """头像占位：圆角方形（方方正正一大块贴在桌面上太生硬）。"""
        size = min(image.width, image.height)
        image = image.resize((size, size), Image.BILINEAR)
        mask = Image.new("L", (size, size), 0)
        ImageDraw.Draw(mask).rounded_rectangle(
            (0, 0, size - 1, size - 1), radius=max(8, size // 12), fill=255
        )
        out = Image.new("RGBA", (size, size), (0, 0, 0, 0))
        out.paste(image, (0, 0), mask)
        return out

    def _fitted_portrait(self, area: R.Rect) -> Image.Image | None:
        """按区域大小缩好的立绘（**按 (朋友, 情绪, 尺寸) 缓存**）。

        2026-09-27（用户："角色情绪转变的时候，换动作表情会特别卡顿特别慢"）：
        缓存命中时**连解码都不做** —— 原来换一次表情要"解一张 1536×2048（~51ms）+ resize（~16ms）"，
        现在直接拿缓存的小图。实测：旧路径 ~67ms/次，命中缓存 ≈ 0ms。
        """
        if area.width <= 0 or area.height <= 0:
            return None
        mood_key = (self._friend_id or "", self._mood() or "正常")
        fit_key = (mood_key, area.width, area.height)
        with self._portrait_lock:
            cached = self._portrait_scaled.get(fit_key)
        if cached is not None:
            image, is_avatar = cached
            self._portrait_key = mood_key
            self._portrait_is_avatar = is_avatar
            return image
        base = self._load_portrait()
        if base is None:
            return None
        ratio = min(area.width / base.width, area.height / base.height)
        image = base.resize(
            (max(1, int(base.width * ratio)), max(1, int(base.height * ratio))),
            Image.BILINEAR,
        )
        with self._portrait_lock:
            self._portrait_scaled[fit_key] = (image, self._portrait_is_avatar)
            if len(self._portrait_scaled) > 24:  # 别越攒越多（留最近 24 张小图，~15MB）
                for stale in list(self._portrait_scaled)[: len(self._portrait_scaled) - 24]:
                    self._portrait_scaled.pop(stale, None)
        return image

    # ── 立绘动作（D123）────────────────────────────────────────────
    def _note_mood(self) -> None:
        """表情变了就播一次对应的小动作 + 一声音效（换表情 / 翻段时都会走这里）。

        音效只在**真有立绘**时放（D131）：头像占位/首字母色块那种没立绘的情况不放，
        免得"明明是占位还在那儿配音效"。
        """
        mood = self._mood()
        if mood == self._motion_mood:
            return
        self._motion_mood = mood
        name = R.motion_for_mood(mood)
        self._motion_name = None if name == "plain" else name
        self._motion_start = time.monotonic()
        if not self._portrait_is_avatar or self._live2d.active:
            self._play_mood_sound(name)

    def _current_motion(self) -> tuple[float, float, float, float] | None:
        if not self._motion_name:
            return None
        duration = R.MOTIONS[self._motion_name][0]
        elapsed = time.monotonic() - self._motion_start
        if elapsed >= duration:
            self._motion_name = None
            return None
        return R.motion_transform(self._motion_name, elapsed / duration, self._scale)

    def _play_mood_sound(self, motion_name: str) -> None:
        """换表情的"哒"一声（音效文件是 LingChat 那套，见 D131）。**异步播，不挡画面。**"""
        filename = R.MOOD_SOUNDS.get(motion_name)
        if not filename:
            return
        # 打包后音效在解包目录里（RESOURCE_DIR），开发时就是仓库根
        for base in (RESOURCE_DIR / "frontend" / "dist", RESOURCE_DIR / "frontend" / "public"):
            path = base / "sounds" / filename
            if path.is_file():
                try:
                    winsound.PlaySound(
                        str(path),
                        winsound.SND_FILENAME | winsound.SND_ASYNC | winsound.SND_NODEFAULT,
                    )
                except Exception:  # noqa: BLE001
                    logger.debug("表情音效没放出来（忽略）")
                return

    def _tick_motion(self) -> None:
        if self._visible and (self._motion_name or self._live2d.active):
            self._repaint()

    def _request_repaint(self) -> None:
        """UI 定时器只改状态；Live2D 让下一个动画帧统一画，避免重复推进模型。"""
        if self._visible and self._live2d.active:
            return
        self._repaint()

    def _repaint(self) -> None:
        if not self._hwnd:
            return
        text = self._current_text()
        index = max(0, min(self._index, len(self._paragraphs) - 1)) if self._paragraphs else 0
        layout_text = self._paragraphs[index] if self._paragraphs else text
        # 气泡/历史用真实情绪；只有 Live2D 的脸可以用空闲视觉表情（D465）。
        actual_mood = self._mood()
        visual_mood = self._visual_mood()
        layout_key = (
            self._scale,
            self._ui_scale,
            self._name(),
            actual_mood,
            layout_text,
            self._mute,
            self._menu,
            self._locked,
        )
        if self._layout is None or layout_key != self._layout_key:
            visible = None
            if self._hwnd:
                rect = win32.window_rect(self._hwnd)
                work = win32.work_area_of(self._hwnd)
                win_w, win_h = R.window_size(self._scale)
                visible = (
                    max(0, work[0] - rect[0]),
                    max(0, work[1] - rect[1]),
                    min(win_w, work[2] - rect[0]),
                    min(win_h, work[3] - rect[1]),
                )
            layout = R.compute_layout(
                scale=self._scale,
                ui_scale=self._ui_scale,
                locked=self._locked,
                name=self._name(),
                mood=actual_mood,
                text=layout_text,
                display_text=text,
                mute=self._mute,
                menu_open=self._menu,
                visible=visible,
            )
            self._layout_key = layout_key
        else:
            layout = self._layout
        self._layout = layout
        show_buttons = self._buttons_hover and not self._locked
        # 2026-09-27（用户要求）：气泡**只在她说话时显示**，不再"悬停就翻出来"
        show_bubble = (
            bool(self._paragraphs) and self._bubble_shown and not self._locked
        )
        if show_bubble:
            # 气泡的大小按完整段落预留；逐字显示只换行文字，不改变角色位置。
            lines_key = (
                text,
                layout.width,
                layout.bubble.left,
                layout.bubble.top,
                layout.bubble.width,
                layout.ui_scale,
            )
            if lines_key != self._bubble_lines_key:
                self._bubble_lines_cache = R.bubble_lines_for(layout, text)
                self._bubble_lines_key = lines_key
            if layout.bubble_lines != self._bubble_lines_cache:
                layout.bubble_lines = list(self._bubble_lines_cache)
        menu_open = self._menu and not self._locked

        live2d_frame = None
        if self._valid_area(layout.portrait):
            live2d_frame = self._live2d.render(
                friend_id=self._friend_id,
                model_id=self._live2d_model_id,
                mood=visual_mood,
                speaking=bool(
                    self._reveal_left > 0 or time.monotonic() < self._speech_until
                ),
                area_width=layout.portrait.width,
                area_height=layout.portrait.height,
                premultiplied=True,
                fit=False,
            )
        # Live2D 一律走快速路径：气泡 / 悬停按钮 / 菜单只是静态 chrome，
        # 按状态缓存后每帧只把角色贴进常驻 DIB，再把 chrome 叠回角色区域。
        # 以前这些 UI 一出现就走整窗 compose，实测会从 117fps 掉到 51fps。
        if live2d_frame is not None:
            self._present_live2d_fast(
                layout,
                live2d_frame,
                show_buttons=show_buttons,
                show_bubble=show_bubble,
                menu_open=menu_open,
            )
            self._rect = win32.window_rect(self._hwnd)
            if self._input_shown:
                self._sync_input_geometry()
            return

        portrait = (
            self._live2d._fit_to_area(
                live2d_frame,
                layout.portrait.width,
                layout.portrait.height,
            ).convert("RGBA")
            if live2d_frame is not None
            else self._fitted_portrait(layout.portrait)
        )
        chrome = self._get_chrome(
            layout=layout,
            show_buttons=show_buttons,
            show_bubble=show_bubble,
            menu_open=menu_open,
            active_scale=self._scale,
        )
        frame = R.compose_frame(
            layout=layout,
            portrait=portrait,
            show_buttons=show_buttons,
            show_bubble=show_bubble,
            menu_open=menu_open,
            active_scale=self._scale,
            motion=None if live2d_frame is not None else self._current_motion(),
            transform_cache=self._transform_cache,  # 2026-09-27（D213）：动作变换走缓存
            chrome=chrome,
        )
        self._blit(frame)
        self._rect = win32.window_rect(self._hwnd)
        if self._input_shown:
            self._sync_input_geometry()
        if live2d_frame is None:
            self._prefetch_portraits()  # 静态立绘兜底才需要预热

    @staticmethod
    def _valid_area(area: R.Rect) -> bool:
        return area.width > 0 and area.height > 0

    # ── 分层窗口：常驻 DIB + 快速 Live2D 路径 ─────────────────────────
    def _release_gdi(self) -> None:
        if self._gdi_memory_dc:
            if self._gdi_old_bitmap:
                gdi32.SelectObject(self._gdi_memory_dc, self._gdi_old_bitmap)
            if self._gdi_bitmap:
                gdi32.DeleteObject(self._gdi_bitmap)
            gdi32.DeleteDC(self._gdi_memory_dc)
        if self._gdi_screen_dc:
            user32.ReleaseDC(0, self._gdi_screen_dc)
        self._gdi_screen_dc = 0
        self._gdi_memory_dc = 0
        self._gdi_bitmap = 0
        self._gdi_old_bitmap = 0
        self._gdi_bits = None
        self._gdi_buffer = None
        self._gdi_array = None
        self._gdi_size = (0, 0)
        self._fast_base_ready = False
        self._fast_last_box = None
        self._fast_chrome_dirty = []
        self._fast_chrome_base_array = None
        self._fast_chrome_base_key = None
        self._fast_bubble_key = None
        self._fast_bubble_array = None
        self._fast_bubble_origin = (0, 0)
        self._fast_bubble_rect = None
        self._fast_bubble_dirty = None
        self._release_char_gdi()

    def _release_char_gdi(self) -> None:
        if self._char_dc:
            if self._char_old_bitmap:
                gdi32.SelectObject(self._char_dc, self._char_old_bitmap)
            if self._char_bitmap:
                gdi32.DeleteObject(self._char_bitmap)
            gdi32.DeleteDC(self._char_dc)
        self._char_dc = 0
        self._char_bitmap = 0
        self._char_old_bitmap = 0
        self._char_bits = None
        self._char_buffer = None
        self._char_array = None
        self._char_size = (0, 0)

    def _ensure_gdi(self, width: int, height: int) -> None:
        """按窗口尺寸建一次 DIB/DC，之后每帧复用。

        以前每次重画都 CreateDIBSection + CreateCompatibleDC + SelectObject +
        DeleteObject，30fps 下光 GDI 对象就先把自己拖慢了。
        """
        if (width, height) == self._gdi_size and self._gdi_bits:
            return
        self._release_gdi()
        screen_dc = user32.GetDC(0)
        memory_dc = gdi32.CreateCompatibleDC(screen_dc)
        info = BITMAPINFO()
        info.bmiHeader.biSize = ctypes.sizeof(BITMAPINFOHEADER)
        info.bmiHeader.biWidth = width
        info.bmiHeader.biHeight = -height  # 负数 = 自上而下
        info.bmiHeader.biPlanes = 1
        info.bmiHeader.biBitCount = 32
        info.bmiHeader.biCompression = 0
        bits = ctypes.c_void_p()
        bitmap = gdi32.CreateDIBSection(
            screen_dc, ctypes.byref(info), 0, ctypes.byref(bits), None, 0
        )
        previous = gdi32.SelectObject(memory_dc, bitmap)
        self._gdi_screen_dc = screen_dc
        self._gdi_memory_dc = memory_dc
        self._gdi_bitmap = bitmap
        self._gdi_old_bitmap = previous
        self._gdi_bits = int(bits.value or 0)
        # numpy 直接写 DIB 内存：避免每帧 tobytes + 逐行 memmove。
        self._gdi_buffer = (ctypes.c_ubyte * (width * height * 4)).from_address(
            self._gdi_bits
        )
        self._gdi_array = np.frombuffer(
            self._gdi_buffer, dtype=np.uint8
        ).reshape(height, width, 4)
        self._gdi_size = (width, height)
        self._fast_base_ready = False
        self._fast_last_box = None

    def _ensure_char_gdi(self, width: int, height: int) -> None:
        """角色中转 DIB：按需增长，复用同一块内存给 StretchBlt 放大。"""
        if (
            self._char_dc
            and width <= self._char_size[0]
            and height <= self._char_size[1]
        ):
            return
        self._release_char_gdi()
        cap_width = max(256, ((width + 255) // 256) * 256)
        cap_height = max(256, ((height + 255) // 256) * 256)
        screen_dc = self._gdi_screen_dc or user32.GetDC(0)
        memory_dc = gdi32.CreateCompatibleDC(screen_dc)
        info = BITMAPINFO()
        info.bmiHeader.biSize = ctypes.sizeof(BITMAPINFOHEADER)
        info.bmiHeader.biWidth = cap_width
        info.bmiHeader.biHeight = -cap_height
        info.bmiHeader.biPlanes = 1
        info.bmiHeader.biBitCount = 32
        info.bmiHeader.biCompression = 0
        bits = ctypes.c_void_p()
        bitmap = gdi32.CreateDIBSection(
            screen_dc, ctypes.byref(info), 0, ctypes.byref(bits), None, 0
        )
        previous = gdi32.SelectObject(memory_dc, bitmap)
        self._char_dc = memory_dc
        self._char_bitmap = bitmap
        self._char_old_bitmap = previous
        self._char_bits = int(bits.value or 0)
        self._char_buffer = (ctypes.c_ubyte * (cap_width * cap_height * 4)).from_address(
            self._char_bits
        )
        self._char_array = np.frombuffer(
            self._char_buffer, dtype=np.uint8
        ).reshape(cap_height, cap_width, 4)
        self._char_size = (cap_width, cap_height)

    def _write_full(self, data: bytes | np.ndarray) -> None:
        if self._gdi_array is None:
            return
        source = (
            data
            if isinstance(data, np.ndarray)
            else np.frombuffer(data, dtype=np.uint8)
        )
        self._gdi_array.reshape(-1)[:] = source.reshape(-1)

    def _present(self) -> None:
        if not self._hwnd or not self._gdi_memory_dc:
            return
        width, height = self._gdi_size
        rect = win32.window_rect(self._hwnd)
        destination = POINT(rect[0], rect[1])
        size = SIZE(width, height)
        source = POINT(0, 0)
        blend = BLENDFUNCTION(AC_SRC_OVER, 0, 255, AC_SRC_ALPHA)
        user32.UpdateLayeredWindow(
            self._hwnd,
            self._gdi_screen_dc,
            ctypes.byref(destination),
            ctypes.byref(size),
            self._gdi_memory_dc,
            ctypes.byref(source),
            0,
            ctypes.byref(blend),
            ULW_ALPHA,
        )

    def _present_dirty(self, dirty: tuple[int, int, int, int] | None = None) -> None:
        """只让 DWM 更新角色附近的脏矩形。

        分层窗口的 DIB 仍然是整窗大小，但 `UpdateLayeredWindowIndirect` 的
        prcDirty 能告诉系统“只有这块变了”。桌宠在大尺寸下整窗 8~30MB，
        每帧全量提交会把 60fps 直接吃掉；脏矩形只覆盖角色旧/新外接框。
        """
        if dirty is None or not self._hwnd or not self._gdi_memory_dc:
            self._present()
            return
        width, height = self._gdi_size
        rect = win32.window_rect(self._hwnd)
        destination = POINT(rect[0], rect[1])
        size = SIZE(width, height)
        source = POINT(0, 0)
        blend = BLENDFUNCTION(AC_SRC_OVER, 0, 255, AC_SRC_ALPHA)
        x1, y1, x2, y2 = dirty
        x1 = max(0, min(width, int(x1)))
        y1 = max(0, min(height, int(y1)))
        x2 = max(0, min(width, int(x2)))
        y2 = max(0, min(height, int(y2)))
        if x2 <= x1 or y2 <= y1:
            self._present()
            return
        dirty_rect = wintypes.RECT(x1, y1, x2, y2)
        info = UPDATELAYEREDWINDOWINFO()
        info.cbSize = ctypes.sizeof(UPDATELAYEREDWINDOWINFO)
        info.hdcDst = self._gdi_screen_dc
        info.pptDst = ctypes.pointer(destination)
        info.psize = ctypes.pointer(size)
        info.hdcSrc = self._gdi_memory_dc
        info.pptSrc = ctypes.pointer(source)
        info.crKey = 0
        info.pblend = ctypes.pointer(blend)
        info.dwFlags = ULW_ALPHA
        info.prcDirty = ctypes.pointer(dirty_rect)
        user32.UpdateLayeredWindowIndirect(self._hwnd, ctypes.byref(info))

    def _blit(self, frame: Image.Image) -> None:
        """整帧更新（气泡 / 按钮 / 菜单等 UI 变化时走这里）。"""
        width, height = frame.size
        self._ensure_gdi(width, height)
        self._write_full(premultiplied_bgra(frame))
        # 这次 DIB 里已经写入了整帧（含角色），下次快速路径必须先把 chrome
        # 底板重写一遍，不能在旧角色上直接叠新角色。
        self._fast_base_ready = False
        self._fast_last_box = None
        self._present()

    def _fast_chrome(
        self,
        layout: R.Layout,
        *,
        show_buttons: bool,
        menu_open: bool,
    ) -> np.ndarray:
        """快速路径的**静态** UI 底板（不含气泡）。

        气泡是流式文字里唯一每 30ms 变一次的东西；把它单独成层后，
        底板只随按钮/锁/菜单/名字/情绪变化，流式输出时不再整窗 copy + diff。
        """
        base_key = (
            layout.width,
            layout.height,
            layout.scale,
            layout.ui_scale,
            layout.locked,
            layout.name,
            layout.mood,
            layout.muted,
            show_buttons,
            menu_open,
        )
        if base_key != self._fast_chrome_key or self._fast_chrome_array is None:
            overlay = R.render_chrome(
                layout=layout,
                show_buttons=show_buttons,
                show_bubble=False,
                menu_open=menu_open,
                active_scale=self._scale,
            )
            chrome_bytes = premultiplied_bgra(overlay)
            chrome_array = np.frombuffer(
                chrome_bytes, dtype=np.uint8
            ).reshape(layout.height, layout.width, 4)
            self._fast_chrome_array = chrome_array
            self._fast_chrome_base_array = chrome_array
            self._fast_chrome_base_key = base_key
            self._fast_patches = self._chrome_rects(chrome_array)
            self._fast_chrome_key = base_key
            self._fast_base_ready = False
            self._fast_last_box = None
            # 底板换了 → 气泡要重新叠一次
            self._fast_bubble_key = None
            self._fast_bubble_rect = None
            self._fast_bubble_dirty = None
        return self._fast_chrome_array

    def _fast_bubble_layer(
        self,
        layout: R.Layout,
        *,
        show_bubble: bool,
    ) -> np.ndarray | None:
        """气泡小层：文字变了才重画，返回预乘 BGRA 数组。

        `self._fast_bubble_dirty` 会记录这一帧需要恢复/重贴的矩形，
        由 `_present_live2d_fast` 合并进最终脏区。
        """
        if not show_bubble or not layout.bubble_lines:
            old = self._fast_bubble_rect
            self._fast_bubble_key = None
            self._fast_bubble_array = None
            self._fast_bubble_rect = None
            if old is not None:
                self._fast_bubble_dirty = old
            return None
        key = (
            layout.width,
            layout.height,
            layout.bubble.left,
            layout.bubble.top,
            layout.bubble.width,
            layout.bubble.height,
            layout.ui_scale,
            layout.mood,
            tuple(layout.bubble_lines),
        )
        if key == self._fast_bubble_key and self._fast_bubble_array is not None:
            return self._fast_bubble_array
        image, origin = R.render_bubble_layer(layout)
        array = np.frombuffer(
            premultiplied_bgra(image), dtype=np.uint8
        ).reshape(image.height, image.width, 4)
        old = self._fast_bubble_rect
        left, top = origin
        rect = (left, top, left + image.width, top + image.height)
        if old is not None and old != rect:
            dirty = (
                min(old[0], rect[0]),
                min(old[1], rect[1]),
                max(old[2], rect[2]),
                max(old[3], rect[3]),
            )
        else:
            dirty = rect
        self._fast_bubble_key = key
        self._fast_bubble_array = array
        self._fast_bubble_origin = origin
        self._fast_bubble_rect = rect
        self._fast_bubble_dirty = dirty
        return array

    @staticmethod
    def _chrome_rects(chrome: np.ndarray) -> list[R.Rect]:
        """把 chrome 的非透明行段压成几块小矩形，避免每帧扫整块角色区。"""
        return PetNativeWindow._chrome_mask_rects(chrome[:, :, 3] > 0)

    @staticmethod
    def _chrome_mask_rects(mask: np.ndarray) -> list[R.Rect]:
        rows = np.flatnonzero(mask.max(axis=1))
        if len(rows) == 0:
            return []
        result: list[R.Rect] = []
        start = int(rows[0])
        last = start
        for row in rows[1:]:
            current = int(row)
            if current > last + 1:
                result.append(PetNativeWindow._chrome_band(mask, start, last))
                start = current
            last = current
        result.append(PetNativeWindow._chrome_band(mask, start, last))
        return result

    @staticmethod
    def _chrome_band(mask: np.ndarray, top: int, bottom: int) -> R.Rect:
        band = mask[top : bottom + 1, :]
        cols = np.flatnonzero(band.max(axis=0))
        return R.Rect(int(cols[0]), top, int(cols[-1]) + 1, bottom + 1)

    def _get_chrome(
        self,
        *,
        layout: R.Layout,
        show_buttons: bool,
        show_bubble: bool,
        menu_open: bool,
        active_scale: float,
    ) -> Image.Image:
        """慢路径的 UI 覆盖层缓存：文字/悬停/菜单没变就复用上一张。"""
        key = (
            layout.width,
            layout.height,
            layout.scale,
            layout.ui_scale,
            layout.locked,
            layout.name,
            layout.mood,
            layout.muted,
            tuple(layout.bubble_lines),
            show_buttons,
            show_bubble,
            menu_open,
            round(active_scale, 4),
        )
        if key != self._chrome_key or self._chrome_image is None:
            self._chrome_image = R.render_chrome(
                layout=layout,
                show_buttons=show_buttons,
                show_bubble=show_bubble,
                menu_open=menu_open,
                active_scale=active_scale,
            )
            self._chrome_key = key
        return self._chrome_image

    def _restore_chrome_rect(self, rect: tuple[int, int, int, int]) -> None:
        """把一块区域恢复成 chrome 底板（用于清掉上一帧角色留下的边）。"""
        if self._fast_chrome_array is None or self._gdi_array is None:
            return
        width, height = self._gdi_size
        left = max(0, min(width, rect[0]))
        top = max(0, min(height, rect[1]))
        right = max(left, min(width, rect[2]))
        bottom = max(top, min(height, rect[3]))
        if right <= left or bottom <= top:
            return
        self._gdi_array[top:bottom, left:right] = self._fast_chrome_array[
            top:bottom, left:right
        ]

    def _overlay_bubble_rect(self, rect: tuple[int, int, int, int]) -> None:
        """把缓存好的气泡小图按预乘 alpha 叠回指定区域。"""
        if (
            self._fast_bubble_array is None
            or self._gdi_array is None
            or self._fast_bubble_rect is None
        ):
            return
        window_width, window_height = self._gdi_size
        left, top = self._fast_bubble_origin
        bubble_right = left + self._fast_bubble_array.shape[1]
        bubble_bottom = top + self._fast_bubble_array.shape[0]
        x1 = max(left, rect[0], 0)
        y1 = max(top, rect[1], 0)
        x2 = min(bubble_right, rect[2], window_width)
        y2 = min(bubble_bottom, rect[3], window_height)
        if x2 <= x1 or y2 <= y1:
            return
        source = self._fast_bubble_array[
            y1 - top : y2 - top, x1 - left : x2 - left
        ].astype(np.uint16)
        dest = self._gdi_array[y1:y2, x1:x2].astype(np.uint16)
        inv_alpha = 255 - source[:, :, 3:4]
        dest[:, :, :3] = (
            source[:, :, :3]
            + (dest[:, :, :3] * inv_alpha + 127) // 255
        )
        dest[:, :, 3:4] = (
            source[:, :, 3:4]
            + (dest[:, :, 3:4] * inv_alpha + 127) // 255
        )
        self._gdi_array[y1:y2, x1:x2] = dest.astype(np.uint8)

    def _stage_char(self, char: Image.Image) -> np.ndarray:
        """把角色写进中转 DIB（BGRA 预乘），返回中转区视图。"""
        char_width, char_height = char.size
        self._ensure_char_gdi(char_width, char_height)
        if self._char_array is None:
            return np.empty((0, 0, 4), dtype=np.uint8)
        # 让 Pillow 在 C 里一次性完成 RGBA→BGRA；比 np.asarray + 四次通道
        # 赋值快（PIL 的 __array_interface__ 内部同样要 tobytes，会多拷一遍）。
        source = np.frombuffer(
            char.tobytes("raw", "BGRa"), dtype=np.uint8
        ).reshape(char_height, char_width, 4)
        target = self._char_array[:char_height, :char_width]
        target[:] = source
        return target

    def _overlay_chrome_patch(
        self,
        patch: R.Rect,
        char_array: np.ndarray,
        char_box: tuple[int, int, int, int],
    ) -> None:
        """把一小块 UI（锁定键）按预乘 alpha 叠到角色上。"""
        if self._fast_chrome_array is None or self._gdi_array is None:
            return
        window_width, window_height = self._gdi_size
        left, top, right, bottom = char_box
        x1 = max(left, patch.left, 0)
        y1 = max(top, patch.top, 0)
        x2 = min(right, patch.right, window_width)
        y2 = min(bottom, patch.bottom, window_height)
        if x2 <= x1 or y2 <= y1:
            return
        char_sub = char_array[
            y1 - top : y2 - top, x1 - left : x2 - left
        ].astype(np.uint16)
        chrome_sub = self._fast_chrome_array[y1:y2, x1:x2].astype(np.uint16)
        chrome_alpha = chrome_sub[:, :, 3:4]
        output = chrome_sub.copy()
        output[:, :, :3] = (
            chrome_sub[:, :, :3]
            + (char_sub[:, :, :3] * (255 - chrome_alpha) + 127) // 255
        )
        output[:, :, 3:4] = (
            chrome_alpha
            + (char_sub[:, :, 3:4] * (255 - chrome_alpha) + 127) // 255
        )
        self._gdi_array[y1:y2, x1:x2] = output.astype(np.uint8)

    def _present_live2d_fast(
        self,
        layout: R.Layout,
        char: Image.Image,
        *,
        show_buttons: bool,
        show_bubble: bool,
        menu_open: bool,
    ) -> None:
        """角色每帧只贴角色区域；气泡/按钮/菜单作为缓存 chrome 叠回。"""
        chrome = self._fast_chrome(
            layout,
            show_buttons=show_buttons,
            menu_open=menu_open,
        )
        self._ensure_gdi(layout.width, layout.height)
        bubble = self._fast_bubble_layer(layout, show_bubble=show_bubble)
        dirty: tuple[int, int, int, int] | None
        if not self._fast_base_ready:
            self._write_full(chrome)
            self._fast_base_ready = True
            self._fast_last_box = None
            dirty = (0, 0, layout.width, layout.height)
        else:
            dirty = None
            if self._fast_chrome_dirty:
                for rect in self._fast_chrome_dirty:
                    self._gdi_array[rect.top : rect.bottom, rect.left : rect.right] = (
                        chrome[rect.top : rect.bottom, rect.left : rect.right]
                    )
                    if dirty is None:
                        dirty = (rect.left, rect.top, rect.right, rect.bottom)
                    else:
                        dirty = (
                            min(dirty[0], rect.left),
                            min(dirty[1], rect.top),
                            max(dirty[2], rect.right),
                            max(dirty[3], rect.bottom),
                        )
                self._fast_chrome_dirty = []

        # 气泡只在文字/情绪变化时重贴这一小块；平时完全不动它。
        if self._fast_bubble_dirty:
            bubble_dirty = self._fast_bubble_dirty
            self._fast_bubble_dirty = None
            self._restore_chrome_rect(bubble_dirty)
            if bubble is not None:
                self._overlay_bubble_rect(bubble_dirty)
            if dirty is None:
                dirty = bubble_dirty
            else:
                dirty = (
                    min(dirty[0], bubble_dirty[0]),
                    min(dirty[1], bubble_dirty[1]),
                    max(dirty[2], bubble_dirty[2]),
                    max(dirty[3], bubble_dirty[3]),
                )

        char_width, char_height = char.size
        if char_width <= 0 or char_height <= 0:
            self._present()
            return
        # 渲染器给的是“刚好裁掉透明边”的小图；这里用 GDI StretchBlt 在窗口
        # DIB 里放大。大尺寸桌宠下，PIL 每帧重采样 5~8MP 会直接掉到 30fps。
        fit = min(
            layout.portrait.width / char_width,
            layout.portrait.height / char_height,
        )
        dest_width = max(1, int(char_width * fit))
        dest_height = max(1, int(char_height * fit))
        left = layout.portrait.left + (layout.portrait.width - dest_width) // 2
        top = layout.portrait.bottom - dest_height
        box = (left, top, left + dest_width, top + dest_height)
        if self._fast_last_box is not None and self._fast_last_box != box:
            self._restore_chrome_rect(self._fast_last_box)
            old = self._fast_last_box
            dirty = (
                min(old[0], box[0]),
                min(old[1], box[1]),
                max(old[2], box[2]),
                max(old[3], box[3]),
            )
        elif dirty is None:
            dirty = box

        self._stage_char(char)
        gdi32.SetStretchBltMode(ctypes.c_void_p(self._gdi_memory_dc), 3)
        gdi32.StretchBlt(
            ctypes.c_void_p(self._gdi_memory_dc),
            left,
            top,
            dest_width,
            dest_height,
            ctypes.c_void_p(self._char_dc),
            0,
            0,
            char_width,
            char_height,
            SRCCOPY,
        )
        char_array = self._gdi_array[top : top + dest_height, left : left + dest_width]
        # 只把真正有内容的 chrome 小块叠到角色区域上；气泡/悬停按钮/菜单
        # 必须在角色上方，但不能每帧拿整块角色区做 alpha 合成。
        for patch in self._fast_patches:
            self._overlay_chrome_patch(patch, char_array, box)
        self._present_dirty(dirty)
        self._fast_last_box = box

    # ── 鼠标 ────────────────────────────────────────────────────────
    @staticmethod
    def _xy(lparam: int) -> tuple[int, int]:
        return (
            ctypes.c_short(lparam & 0xFFFF).value,
            ctypes.c_short((lparam >> 16) & 0xFFFF).value,
        )

    def _track_leave(self) -> None:
        track = TRACKMOUSEEVENT(ctypes.sizeof(TRACKMOUSEEVENT), 0x00000002, self._hwnd, 0)
        user32.TrackMouseEvent(ctypes.byref(track))

    def _on_move(self, point: tuple[int, int]) -> None:
        """鼠标在本窗口上动。**全程用客户端坐标** —— 不涉及任何单位换算。"""
        self._track_leave()
        if self._locked:
            return
        self._mark_hover(point)
        if self._drag is not None:
            ratio = float(self._drag.get("ratio") or 1.0)
            cursor = POINT()
            user32.GetCursorPos(ctypes.byref(cursor))
            cursor_x = cursor.x * ratio
            cursor_y = cursor.y * ratio
            dx = cursor_x - self._drag["cursor0"][0] * ratio
            dy = cursor_y - self._drag["cursor0"][1] * ratio
            if not self._drag["moved"] and abs(dx) + abs(dy) < 4:
                return
            self._drag["moved"] = True
            self._menu = False
            if self._drag.get("resize"):
                # 离窗口左上角的距离（窗口左上角在缩放时不动的）
                distance = max(4.0, math.hypot(cursor_x - self._drag["x"], cursor_y - self._drag["y"]))
                scale = resized_scale(
                    self._drag["scale0"], distance, self._drag["dist0"]
                )
                if abs(scale - self._scale) > 0.001:
                    self._set_scale_on_thread(scale)
                return
            x = int(self._drag["x"] + dx)
            y = int(self._drag["y"] + dy)
            # ⚠️ 别带 HWND_TOPMOST：拖动时反复改 z-order 很贵（顿的另一个来源），
            # 窗口本来就是 topmost，挪位置用 SWP_NOZORDER 就够
            user32.SetWindowPos(
                self._hwnd, 0, x, y, 0, 0, SWP_NOSIZE | SWP_NOZORDER | SWP_NOACTIVATE
            )
            self._pos = (x, y)
            self._rect = win32.window_rect(self._hwnd)
            self._sync_input_geometry()
            return

    def _mark_hover(self, point: tuple[int, int]) -> None:
        """鼠标消息说"我在窗口上" → 悬停成立；顺便记下单位倍率给定时器兜底用。"""
        if self._locked:
            return
        layout = self._layout
        if layout is None or self._drag is not None:
            # 拖动 / 缩放时**不重画**（D123：分层窗口挪位置本来就不用重画，
            # 之前每动一下就重画一遍，就是"顿顿的"根源）
            return
        # 2026-09-27（用户要求）：悬浮判定**只认立绘实体 + 按钮**（透明背景不算）。
        # 统一走 `R.chrome_hit`（跟定时器兜底那路用同一套，省得两边结论不一致 → 闪）；
        # 给一点余量：鼠标"够到"按钮就连着显示，不在缝里忽隐忽现（用户第二次反馈）
        over = R.chrome_hit(layout, point[0], point[1], pad=max(4, int(6 * layout.ui_scale)))
        if not self._hovered or over != self._buttons_hover:
            self._hovered = True
            self._buttons_hover = over
            self._request_repaint()
        self._sync_input_visibility()
        self._calibrate_cursor(point)

    def _calibrate_cursor(self, point: tuple[int, int]) -> None:
        """用一次真实的鼠标移动，标定"光标坐标 → 物理坐标"的倍率。

        为什么必须实测：`GetCursorPos` 和 `GetWindowRect` 各自在哪个坐标系，取决于进程
        是不是 DPI 感知（D70 的老坑）。我之前按 `GetDpiForWindow` 猜，真机里猜反了，
        结果悬停判断永远"不在里面"（表现：只有鼠标动的那一瞬才闪出按钮）。
        """
        if self._cursor_ratio is not None:
            return
        rect = self._rect or win32.window_rect(self._hwnd)
        probe = POINT()
        user32.GetCursorPos(ctypes.byref(probe))
        if probe.x <= 0 or probe.y <= 0:
            return
        ratio_x = (rect[0] + point[0]) / probe.x
        ratio_y = (rect[1] + point[1]) / probe.y
        ratio = (ratio_x + ratio_y) / 2
        if 0.2 <= ratio <= 20:
            self._cursor_ratio = ratio

    def _on_press(self, point: tuple[int, int]) -> None:
        rect = self._rect or win32.window_rect(self._hwnd)
        target = self._target_at(point)
        if target == "button:lock":
            # 锁定按钮是即时开关，不参与拖动。
            self._button("lock")
            return
        if self._locked:
            return
        # ⚠️ 位移必须用**屏幕坐标差**：用客户端坐标差会抖 ——
        # 窗口一移动，同一个鼠标点相对窗口就跟着变了，两边互相追（真机实测"抖"）。
        self._calibrate_cursor(point)  # 顺手用这次点击标定"光标坐标 → 物理坐标"
        cursor = POINT()
        user32.GetCursorPos(ctypes.byref(cursor))
        drag: dict[str, Any] = {
            "cursor0": (cursor.x, cursor.y),
            "ratio": self._cursor_ratio or 1.0,
            "x": rect[0],
            "y": rect[1],
            "target": target,
            "moved": False,
        }
        if target == "resize":
            # 自由缩放（D122）：从窗口左上角算距离，拖着按比例变（客户端坐标，单位一致）
            drag["resize"] = True
            drag["scale0"] = self._scale
            drag["dist0"] = max(4.0, math.hypot(point[0], point[1]))
        self._drag = drag
        user32.SetCapture(self._hwnd)

    def _on_release(self, point: tuple[int, int]) -> None:
        drag = self._drag
        self._drag = None
        user32.ReleaseCapture()
        if drag is None:
            return
        if drag["moved"]:
            self._last_paint = 0.0
            self._layout_key = None
            self._request_repaint()
            self._remember_position()
            self._save_state()
            # 拖完光标还停在桌宠身上 → 立刻把悬停状态补上（不然两个框要等下次鼠标移动才出来）
            self._mark_hover(point)
            return
        self._act(drag["target"])

    def _target_at(self, point: tuple[int, int]) -> str | None:
        if self._layout is None:
            return None
        return R.hit_test(
            self._layout,
            point[0],
            point[1],
            hover_buttons=self._buttons_hover,
            menu_open=self._menu,
        )

    def _act(self, target: str | None) -> None:
        if not target:
            if self._menu:
                self._menu = False
                self._request_repaint()
            return
        if target == "advance":
            self._advance()
            return
        if target.startswith("button:"):
            self._menu = False
            self._button(target.split(":", 1)[1])
            return
        if target.startswith("menu:"):
            self._menu_action(target.split(":", 1)[1])

    def _advance(self) -> None:
        self._auto_deadline = 0.0
        if self._index < len(self._paragraphs) - 1:
            self._index += 1
            self._reveal_left = 0
            self._arm_speech(self._paragraphs[self._index])
            self._bubble_shown = True
            self._bubble_hide_at = 0.0
            self._note_mood()
            self._request_repaint()
            self._arm_auto_advance()

    def _button(self, key: str) -> None:
        if key == "lock":
            self._locked = not self._locked
            self._menu = False
            self._input_wanted = False
            self._drag = None
            self._hovered = False
            self._buttons_hover = False
            self._sync_input_visibility()
            self._save_state()
            self._request_repaint()
            return
        if self._locked:
            return
        if key == "main":
            logger.debug("桌宠按钮：主界面（friend={}）", self._friend_id)
            if self._on_open_main:
                try:
                    self._on_open_main(self._friend_id)
                except Exception:  # noqa: BLE001 —— 唤起失败别把桌宠线程带走
                    logger.opt(exception=True).warning("桌宠：唤起主界面失败")
            else:
                logger.warning("桌宠：没有接「回到主界面」的回调（外壳没给）")
        elif key == "game":
            logger.debug("桌宠按钮：游戏管理（friend={}）", self._friend_id)
            if self._on_open_game:
                try:
                    self._on_open_game(self._friend_id)
                except Exception:  # noqa: BLE001 —— 打不开游戏管理别把桌宠线程带走
                    logger.opt(exception=True).warning("桌宠：打开游戏管理失败")
            else:
                logger.warning("桌宠：没有接「游戏管理」的回调（外壳没给）")
        elif key == "talk":
            # 2026-09-27：输入框改成"点了才出来"（不再跟着鼠标走）
            self._open_input()
        elif key == "mute":
            self._mute = not self._mute
            self._request_repaint()
        elif key == "switch":
            self._switch_friend()
        elif key == "hide":
            self._hide_on_thread()
        elif key == "zoom_in":
            self._set_scale_on_thread(self._scale * ZOOM_STEP)
        elif key == "zoom_out":
            self._set_scale_on_thread(self._scale / ZOOM_STEP)

    def _menu_action(self, key: str) -> None:
        if key == "close":
            self._menu = False
            self._request_repaint()
        elif key == "exit":
            self._menu = False
            self._close_on_thread()
        elif key.startswith("scale:"):
            self._menu = False
            self._set_scale_on_thread(float(key.split(":", 1)[1]), throttle=False)

    def _submit_input(self) -> None:
        buffer = ctypes.create_unicode_buffer(4096)
        user32.GetWindowTextW(self._edit_hwnd, buffer, 4096)
        text = buffer.value.strip()
        user32.SetWindowTextW(self._edit_hwnd, "")
        self._input_wanted = False  # 发出去了就收（2026-09-27）
        self._sync_input_visibility()
        if not text or not self._friend_id:
            return
        self._busy = True
        self._request_repaint()
        friend_id = self._friend_id

        def worker() -> None:
            try:
                self._data.send(friend_id, text)
            except Exception:  # noqa: BLE001
                logger.opt(exception=True).warning("桌宠发消息失败")
            finally:
                self._post(self._after_send)

        threading.Thread(target=worker, daemon=True).start()

    def _after_send(self) -> None:
        self._busy = False
        self._sync_data()
        self._request_repaint()

    # ── 数据 ────────────────────────────────────────────────────────
    def _set_poll_interval(self, interval_ms: int) -> None:
        """回复流式进行中时提高历史轮询频率，结束后恢复 2s。"""
        if interval_ms == self._poll_ms:
            return
        self._poll_ms = interval_ms
        if self._hwnd:
            user32.SetTimer(self._hwnd, 1, interval_ms, None)

    def _sync_data(self, initial: bool = False) -> None:
        if self._syncing:
            return
        self._syncing = True

        def worker() -> None:
            friends: list[dict[str, Any]] = []
            messages: list[dict[str, Any]] = []
            friend_id = self._friend_id
            try:
                friends = self._data.friends()
                if friend_id is None or not any(
                    item.get("id") == friend_id for item in friends
                ):
                    present = [item for item in friends if item.get("presence")]
                    pool = present or friends
                    friend_id = str(pool[0]["id"]) if pool else None
                if friend_id:
                    messages = self._data.history(friend_id)
                    # 这些 HTTP 调用一定留在 worker 线程；放在 UI 线程会在
                    # 主聊天流式输出时把桌宠渲染整段卡住（实测出现过 200ms+）。
                    try:
                        self._data.mark_read(friend_id)
                    except Exception:  # noqa: BLE001
                        pass
                    self._sync_tick += 1
                    if self._sync_tick % 15 == 1:
                        try:
                            self._data.set_focus(friend_id)
                        except Exception:  # noqa: BLE001
                            pass
            except Exception:  # noqa: BLE001
                friends, messages = [], []
            finally:
                self._syncing = False
            self._post(
                lambda: self._apply_data(friends, friend_id, messages, initial)
            )

        threading.Thread(target=worker, daemon=True).start()

    def _apply_data(
        self,
        friends: list[dict[str, Any]],
        friend_id: str | None,
        messages: list[dict[str, Any]],
        initial: bool,
    ) -> None:
        changed_friend = friend_id != self._friend_id
        old_model_id = self._live2d_model_id
        if friends:
            self._friends = friends
        self._friend_id = friend_id
        self._live2d_model_id = next(
            (
                (str(item.get("live2d_model_id") or "") or None)
                for item in friends
                if item.get("id") == friend_id
            ),
            None,
        )
        if changed_friend or old_model_id != self._live2d_model_id:
            self._idle_label_key = None
            self._idle_label_cache = []
            self._idle_visual_mood = ""
            self._next_idle_visual_at = 0.0
        if not friend_id:
            self._set_poll_interval(POLL_MS)
            self._request_repaint()
            return
        self._history_messages = messages
        # D463：assistant 正文在流式过程中每秒落一次库。桌宠每 2s 才拉一次的话，
        # 气泡会整段停住，等回复结束才一次出现；检测到 streaming 就切到 400ms。
        streaming = any(
            item.get("role") == "assistant"
            and item.get("stream_status") == "streaming"
            for item in messages[-2:]
        )
        self._set_poll_interval(STREAM_POLL_MS if streaming else POLL_MS)
        old_paragraphs = list(self._paragraphs)
        last_assistant = None
        for message in reversed(messages):
            if message.get("role") == "assistant" and str(message.get("content") or "").strip():
                last_assistant = message
                break
        if last_assistant is None:
            self._paragraphs = []
            self._moods = []
            self._index = 0
        else:
            paragraphs = R.split_paragraphs(str(last_assistant.get("content") or ""))
            new_message = last_assistant.get("id") != self._last_message_id
            self._paragraphs = paragraphs
            self._moods = [str(item) for item in (last_assistant.get("moods") or [])]
            if new_message or changed_friend or initial:
                self._index = 0
                self._reveal_left = len(paragraphs[-1]) if paragraphs else 0
                self._arm_speech(paragraphs[0] if paragraphs else "", last_assistant, 0)
                self._bubble_shown = True
                self._bubble_hide_at = 0.0
            else:
                # 同一条消息的正文变长了 → 把新增字符也放进"逐字显示"队列，
                # 不要因为 reveal_left 已经归零就整段瞬移出来。
                old_last = old_paragraphs[-1] if old_paragraphs else ""
                new_last = paragraphs[-1] if paragraphs else ""
                if new_last != old_last:
                    if old_last and new_last.startswith(old_last):
                        self._reveal_left += len(new_last) - len(old_last)
                    elif not old_last:
                        self._reveal_left += len(new_last)
                    else:
                        self._reveal_left = 0
                    self._bubble_shown = True
                    self._bubble_hide_at = 0.0
            self._last_message_id = last_assistant.get("id")
            self._maybe_play(last_assistant)
        self._note_mood()
        self._request_repaint()

    def _main_window_in_front(self) -> bool:
        """主窗口（pywebview 那个）是不是**当前的前台窗口**？（v3.14 / D182）

        为什么要它：语音有三个玩家 —— 普通聊天那条语音条 / 沉浸·通话那一套 / 桌宠。
        规矩是"**声音跟着你正在看的那一屏走**"：主窗口在前台 → 页面自己会放，
        桌宠就别插嘴（不然两段一模一样的声音叠在一起，用户实测过）。

        判法：前台窗口的**进程**是我们自己（外壳进程，主窗口和桌宠在同一个进程里），
        而且不是桌宠这几个窗口 → 那就是主窗口。
        读不到就返回 False（照旧播，宁可响也别哑）。
        """
        try:
            foreground = user32.GetForegroundWindow()
            if not foreground:
                return False
            pid = wintypes.DWORD()
            user32.GetWindowThreadProcessId(foreground, ctypes.byref(pid))
            if pid.value != os.getpid():
                return False
            return foreground not in (self._hwnd, self._input_hwnd, self._edit_hwnd)
        except Exception:  # noqa: BLE001 —— 判不出来就当"不在前台"
            return False

    def _maybe_play(self, message: dict[str, Any]) -> None:
        """有语音就**排队**（只在桌宠可见、且没静音时）。

        ⚠️ D134：以前是"每条消息起一个线程去播"，两条回复挨得近就会**叠着响**（用户："语音是乱的"）；
        而且气泡按"估算时长"自己往前走，跟音频各走各的。
        现在：全部丢进**一条队列**、由**唯一的播放线程**顺序放；**每放完一段就通知主线程翻页** ——
        "她说一句、气泡走一句"，跟沉浸模式一个节奏。
        """
        message_id = str(message.get("id") or "")
        if not message_id:
            return
        urls = [
            str(segment.get("url"))
            for segment in (message.get("audio_segments") or [])
            if segment.get("url")
        ]
        if not urls and message.get("audio_url"):
            urls = [str(message["audio_url"])]
        if not urls or self._mute or not self._visible:
            return
        # v3.14（D182）：主窗口在前台 → 这一条**归页面**（它自己会放），桌宠静音；
        # 而且要记下来：窗口后来切走了也不能补播（不然就成两段了）。
        if self._main_window_in_front():
            self._page_owned.add(message_id)
            return
        if message_id in self._page_owned:
            return
        queued = self._spoken.setdefault(message_id, set())
        for index, url in enumerate(urls):
            if index in queued:
                continue
            queued.add(index)
            self._voice_queue.put((message_id, index, url))
        self._ensure_voice_thread()

    def _ensure_voice_thread(self) -> None:
        if self._voice_thread and self._voice_thread.is_alive():
            return
        self._voice_thread = threading.Thread(
            target=self._voice_worker, name="yumefriend-pet-voice", daemon=True
        )
        self._voice_thread.start()

    def _voice_worker(self) -> None:
        """顺序播（winsound 不传 SND_ASYNC 就是阻塞的）→ 每段播完通知主线程翻页。"""
        while True:
            message_id, index, url = self._voice_queue.get()
            try:
                chunk = self._data.audio(url)
                if chunk:
                    winsound.PlaySound(chunk, winsound.SND_MEMORY | winsound.SND_NODEFAULT)
            except Exception:  # noqa: BLE001
                logger.debug("桌宠播音失败（跳过这一段）")
            finally:
                self._post(lambda: self._on_segment_spoken(message_id, index))

    def _on_segment_spoken(self, message_id: str, index: int) -> None:
        """某一段播完了 → 如果它正是显示中的那一段，就翻到下一段（气泡跟着声音走）。"""
        if message_id != self._last_message_id:
            return
        if index == self._index and self._index < len(self._paragraphs) - 1:
            self._auto_deadline = 0.0
            self._index += 1
            self._reveal_left = len(self._paragraphs[self._index])
            self._arm_speech(self._paragraphs[self._index])
            self._bubble_shown = True
            self._bubble_hide_at = 0.0
            self._note_mood()
            self._request_repaint()

    def _tick_reveal(self) -> None:
        if self._reveal_left > 0:
            self._reveal_left = max(0, self._reveal_left - REVEAL_CHARS)
            self._request_repaint()
            if self._reveal_left == 0:
                self._arm_auto_advance()
        if self._bubble_hide_at and time.monotonic() >= self._bubble_hide_at:
            self._bubble_hide_at = 0.0
            self._bubble_shown = False
            self._request_repaint()
        elif self._auto and self._auto_deadline and time.monotonic() >= self._auto_deadline:
            # 自动模式（D124）：桌宠默认一直往下说，不用点
            self._auto_deadline = 0.0
            if self._index < len(self._paragraphs) - 1 and not self._busy:
                self._index += 1
                self._reveal_left = len(self._paragraphs[self._index])
                self._arm_speech(self._paragraphs[self._index])
                self._note_mood()
                self._request_repaint()

    def _arm_auto_advance(self) -> None:
        """这一段说完了 → 排一个"什么时候自动翻到下一段"。

        有语音就按那一段的时长（再多留 0.8s 换气），没语音就等 3.5 秒。
        """
        if not self._auto or self._busy or self._index >= len(self._paragraphs) - 1:
            self._auto_deadline = 0.0
            if not self._busy and self._paragraphs:
                # 最后一段也说完了 → 排一个"收起来"的时间
                self._bubble_hide_at = time.monotonic() + BUBBLE_IDLE_SECONDS
            return
        seconds = AUTO_ADVANCE_IDLE_SECONDS
        try:
            message = None
            for item in reversed(self._history_messages):
                if item.get("role") == "assistant" and str(item.get("content") or "").strip():
                    message = item
                    break
            segments = (message or {}).get("audio_segments") or []
            if self._index < len(segments):
                duration = segments[self._index].get("duration_ms")
                if duration:
                    seconds = float(duration) / 1000.0 + 0.8
        except Exception:  # noqa: BLE001
            pass
        self._auto_deadline = time.monotonic() + max(1.2, seconds)

    def _tick_hover(self) -> None:
        """鼠标在不在桌宠范围里 / 在不在立绘上？

        **主路是鼠标消息**（`WM_MOUSEMOVE` 带客户端坐标，单位天然一致；离开靠 WM_MOUSELEAVE）。
        这个定时器只是**兜底**：鼠标停住不动时再确认一下状态。

        ⚠️ 倍率以 `_cursor_ratio`（一次真实鼠标移动标定）为准；
        还没标定时**先用 1.0 顶上**（2026-10-03：以前这里直接 return，结果"按钮挪左边"那次
        鼠标一进透明区、兜底也罢工 → 按钮再也回不来）。
        """
        if not self._visible or self._menu or self._drag is not None or self._layout is None:
            return
        ratio = self._cursor_ratio if self._cursor_ratio is not None else 1.0
        point = POINT()
        user32.GetCursorPos(ctypes.byref(point))
        rect = self._rect or win32.window_rect(self._hwnd)
        cursor_x = point.x * ratio
        cursor_y = point.y * ratio
        inside = rect[0] <= cursor_x < rect[2] and rect[1] <= cursor_y < rect[3]
        layout = self._layout
        x, y = cursor_x - rect[0], cursor_y - rect[1]
        # 跟 `_mark_hover` 用**同一个**函数（以前这里用"整块立绘区"、那边用"立绘实体框"
        # → 鼠标在透明边那带来回横跳 → 按钮"闪呀闪"还点不到；用户 2026-09-27 报的）
        over_portrait = inside and R.chrome_hit(layout, x, y, pad=max(4, int(6 * layout.ui_scale)))
        # 悬停按钮**别一闪一闪**（D130）：鼠标贴着边界抖一下、或者刚移到按钮上时，
        # 判定会在"在/不在"之间反复横跳 → 给 350ms 宽容期，这期间按钮一直留着。
        now = time.monotonic()
        if over_portrait:
            self._hover_grace_until = now + HOVER_GRACE_SECONDS
        elif now < self._hover_grace_until:
            over_portrait = True
        if not inside:
            self._hover_grace_until = 0.0   # 真出去了才立刻收
        if inside != self._hovered or over_portrait != self._buttons_hover:
            self._hovered = inside
            self._buttons_hover = over_portrait
            self._request_repaint()
        self._sync_input_visibility()

    def _sync_input_visibility(self) -> None:
        """输入小窗：**点了「说话」/ 正在打字 / 里面有字**才显示（不再跟着鼠标走，2026-09-27）。"""
        if not self._input_hwnd:
            return
        buffer = ctypes.create_unicode_buffer(256)
        user32.GetWindowTextW(self._edit_hwnd, buffer, 256)
        focused = user32.GetFocus() == self._edit_hwnd
        has_text = bool(buffer.value.strip())
        if not focused and not has_text:
            self._input_wanted = False  # 失焦又没字 → 收（用户要求）
        want = (
            self._visible
            and not self._locked
            and (self._input_wanted or focused or has_text)
        )
        if want == self._input_shown:
            return
        self._input_shown = want
        if want:
            user32.ShowWindow(self._input_hwnd, SW_SHOWNOACTIVATE)
            user32.SetWindowPos(
                self._input_hwnd, HWND_TOPMOST, 0, 0, 0, 0, SWP_NOMOVE | SWP_NOSIZE
            )
        else:
            user32.ShowWindow(self._input_hwnd, SW_HIDE)

    def _open_input(self) -> None:
        """点「说话」图标 → 显示 + 聚焦输入框（2026-09-27）。"""
        if self._locked:
            return
        self._input_wanted = True
        self._sync_input_visibility()
        try:
            user32.SetFocus(self._edit_hwnd)
        except Exception:  # noqa: BLE001 —— 抢不到焦点也没关系，框先显示出来
            pass

    def _transparent_at(self, lparam: int) -> bool:
        """这块鼠标要不要**穿过去**？（用户 2026-09-27："聊天框不该在点击范围里"）

        只认"气泡那条带"（立绘上方，`y < portrait.top`）—— 那儿除了气泡什么都没有
        （菜单打开时例外，菜单就画在那条带上）。返回 True → 外壳回 HTTRANSPARENT。
        """
        if self._layout is None or self._menu:
            return False
        y = ctypes.c_short((lparam >> 16) & 0xFFFF).value
        rect = self._rect or win32.window_rect(self._hwnd)
        return (y - rect[1]) < self._layout.portrait.top

    def _switch_friend(self) -> None:
        if self._locked:
            return
        # 2026-09-27（D213）：优先在"在线"的朋友里轮换，但**在线不足两个就退回全部**
        # —— 以前 `[...presence] or self._friends` 在"只有一个在线"时池子只有 1 个，
        # 直接 return → 按钮点了没反应（用户实测报的）。
        present = [item for item in self._friends if item.get("presence")]
        pool = present if len(present) >= 2 else self._friends
        if len(pool) < 2 or not self._friend_id:
            logger.debug(
                "桌宠换朋友：池子不够（在线的 {} 个 / 一共 {} 个）", len(present), len(self._friends)
            )
            return
        ids = [str(item.get("id")) for item in pool]
        try:
            index = ids.index(self._friend_id)
        except ValueError:
            index = -1
        self._friend_id = ids[(index + 1) % len(ids)]
        logger.debug("桌宠换朋友 → {}", self._friend_id)
        self._paragraphs = []
        self._moods = []
        self._index = 0
        self._buttons_hover = False
        self._menu = False
        self._portrait_key = None
        self._request_repaint()
        self._save_state()
        self._sync_data()
        self._prefetch_portraits()  # 换了朋友 → 预热她的立绘（2026-09-27）
