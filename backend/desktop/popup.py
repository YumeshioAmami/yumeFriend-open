"""朋友主动冒泡 / 提醒到点时，弹一张**我们自己画的卡片**（D70）。

**为什么不用 Windows 系统通知**（用户 2026-09-19 提的）：
· 系统通知在 WebView2 里权限说不清（用户实测"点一下就没了"），样式也完全不受控；
· 我们要的是"朋友在敲你"的感觉，不是"某个程序发了一条通知"。

做法：pywebview 的第二个窗口 —— 无边框 + 置顶 + 不抢焦点，
压在任务栏上方的右下角，几秒后自己消失；点一下打开主窗口并跳到那个朋友。

**实测踩过的坑（2026-09-19，都在真机上量过）**：

0. **别用 `transparent=True` 去要圆角**（第一版就是这么翻车的）：
   它只让 WebView 透明，**窗口本身的底还是 WinForms 的默认浅灰 `#F0F0F0`**，
   于是卡片四周和四个角都露出一圈灰白边（实拍像素：`E8E8E9` / `F0F0F0`）。
   正确做法：**窗口做成不透明**，交给 Windows 11 的 DWM 去圆角
   （`DWMWA_WINDOW_CORNER_PREFERENCE = ROUND`）+ `shadow=True` 拿系统阴影。
   这样圆角外是 DWM 负责的真透明，还自带抗锯齿，不用我们去抠像素。
1. **create_window 的宽高算不准**：传 372x104，实际内容区只有 358x67
   （显示器 150% 缩放，位置和尺寸用的还不是同一套换算）。
   好在 **`resize()` 是按 CSS 像素来的、精确**：`resize(386,141)` → 内容区正好 386x141。
   所以建完窗口立刻 `resize(WIDTH, HEIGHT)` 校正一次。
2. **位置用 `move()`**：实测 `move(1000,500)` → 物理 (1500,750)，跟 create 的坐标系一致，
   而且**和任务栏/DPI 都无关**：不管进程是不是 DPI 感知，
   "工作区右下角 - 我的尺寸 - 边距"都成立（150% 缩放时工作区会同时缩成虚拟坐标）。
3. **`hidden=True` 不太灵**：窗口建出来就是可见的，得自己 `hide()` 一次兜住。
4. **任务栏**：无边框窗口照样会在任务栏留一个按钮，
   得自己贴上 `WS_EX_TOOLWINDOW` 摘掉。
5. **别用 `window.show()`**：它会抢焦点（正在打字时被夺走很讨厌），
   用 Win32 `ShowWindow(SW_SHOWNOACTIVATE)` 只露出来、不激活。
6. **必须等页面加载完再露出来**：WebView2 的合成器在加载完成前接不上，
   这时候把窗口显示出来就是**一片白**
   （实测：显示后 1s / 3s 都全白，手动 hide→show 一次立刻正常）。
   所以建完窗口先藏着，等 `events.loaded` 到了再摆位置 + ShowWindow。
   （pywebview 源码里那句 "no idea why this works" 的 Show/Hide 折腾，就是这个坑。）
"""

from __future__ import annotations

import html
import threading
import time
from typing import Any

from loguru import logger

from backend.desktop import win32

TITLE = "yumeFriendPop"

# 逻辑（CSS）像素 —— 实际物理尺寸按当前 DPI 换算
WIDTH = 372
HEIGHT = 104
MARGIN = 18
SHOW_SECONDS = 6.0
MAX_TEXT = 200

# 卡片底色：**必须是不透明色**，而且页面底色要跟窗口底色一致
#（窗口不透明，圆角和阴影交给 Windows 的 DWM，见模块开头第 0 条）
CARD_BG = {"light": "#ffffff", "dark": "#241f19"}

# 气泡和桌宠之间的缝（物理像素）
GAP = 10


def compute_anchor_target(
    *,
    popup_size: tuple[int, int],
    anchor: tuple[int, int, int, int],
    gap: int = GAP,
) -> tuple[int, int]:
    """气泡要摆在"锚点（桌宠）正上方居中"时，它的**左上角**该在哪儿（物理像素）。

    D119 第 6 条：桌宠在的时候，提醒 / 主动消息的气泡弹在桌宠卡片正上方。
    三条规则（纯函数，好测）：

    1. **水平居中**于桌宠，但夹在当前显示器的可用区域里（贴边的桌宠也不许气泡跑出去）；
    2. 默认放上方（`锚点上方 - 缝 - 气泡高`）；
    3. 上方不够（桌宠贴着屏幕顶端）→ **翻到下面**（`锚点下方 + 缝`），再夹一次。
    """
    width, height = popup_size
    left, top, right, bottom = anchor
    work_left, work_top, work_right, work_bottom = win32.work_area_at_point(
        (left + right) // 2, (top + bottom) // 2
    )

    x = (left + right) // 2 - width // 2
    x = max(work_left, min(x, work_right - width))

    y = top - gap - height
    if y < work_top:
        y = bottom + gap
    y = max(work_top, min(y, work_bottom - height))
    return int(x), int(y)


def card_html(*, name: str, text: str, initial: str, dark: bool) -> str:
    """卡片本体。**纯函数**（好测）：只负责拼一段自包含的 HTML。

    ⚠️ 页面底色必须是**不透明**的、而且要跟窗口底色一致：
    窗口已经是不透明窗口了（圆角和阴影由 DWM 负责），
    页面再画一圈半透明/圆角，就会在角上露出窗口底色 = 又变回"白边"。
    """
    body = html.escape(text)
    safe_name = html.escape(name)
    safe_initial = html.escape(initial)
    if dark:
        bg, ink, soft = CARD_BG["dark"], "#f3ece2", "#a1968a"
    else:
        bg, ink, soft = CARD_BG["light"], "#0f172a", "#78716c"
    return f"""<!doctype html><html><head><meta charset="utf-8"><style>
 html,body{{margin:0;height:100%;background:{bg};overflow:hidden;
   font-family:-apple-system,'Segoe UI','Microsoft YaHei',sans-serif;}}
 .card{{height:100%;box-sizing:border-box;display:flex;align-items:center;gap:11px;
   padding:13px 15px;background:{bg};cursor:pointer;}}
 .bar{{width:3px;align-self:stretch;border-radius:2px;background:#c26947;opacity:.9}}
 .avatar{{width:40px;height:40px;border-radius:999px;background:#334155;color:#fff;flex:0 0 auto;
   display:flex;align-items:center;justify-content:center;font-size:15px}}
 .name{{font-size:12px;color:{soft};margin-bottom:3px}}
 .text{{font-size:13.5px;color:{ink};line-height:1.5;
   display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden}}
</style></head><body>
<div class="card" id="card" onclick="try{{window.pywebview.api.open_friend()}}catch(e){{}}">
 <span class="bar"></span>
 <div class="avatar">{safe_initial}</div>
 <div style="min-width:0;flex:1">
  <div class="name">{safe_name} · 刚刚</div>
  <div class="text">{body}</div>
 </div>
</div>
</body></html>"""


class _Api:
    """暴露给卡片页面的两个动作（pywebview 会把公开方法挂到 window.pywebview.api）。"""

    def __init__(self, notifier: "PopupNotifier") -> None:
        self._notifier = notifier

    def open_friend(self) -> None:  # noqa: D102
        self._notifier.clicked()


class PopupNotifier:
    """自己画的弹窗。**懒创建、复用同一个窗口**（每次新建一个 WebView 太慢）。"""

    def __init__(
        self,
        *,
        main_title: str = "yumeFriend",
        on_click: Any = None,
        theme: Any = None,
        anchor: Any = None,
    ) -> None:
        self._main_title = main_title
        self._on_click = on_click
        self._theme = theme
        # D119：返回桌宠窗口的物理矩形 (l,t,r,b)；桌宠没开时返回 None → 回退右下角
        self._anchor = anchor
        self._window: Any = None
        self._hwnd: int = 0
        self._timer: threading.Timer | None = None
        self._lock = threading.Lock()
        self._friend_id: str = ""
        self._loaded = threading.Event()

    # ── 对外：弹一条 ────────────────────────────────────────────────
    def show(self, *, friend_id: str, friend_name: str, text: str) -> None:
        with self._lock:
            try:
                self._show_locked(friend_id=friend_id, friend_name=friend_name, text=text)
            except Exception:  # noqa: BLE001 —— 弹窗失败绝不能影响消息
                logger.opt(exception=True).warning("弹窗失败（已忽略）")

    def clicked(self) -> None:
        self.hide()
        if self._on_click and self._friend_id:
            try:
                self._on_click(self._friend_id)
            except Exception:  # noqa: BLE001
                logger.opt(exception=True).warning("弹窗点击回调失败（已忽略）")

    def hide(self) -> None:
        if self._timer is not None:
            self._timer.cancel()
            self._timer = None
        if self._hwnd:
            try:
                win32.hide_window(self._hwnd)
            except Exception:  # noqa: BLE001
                pass

    # ── 内部 ───────────────────────────────────────────────────────
    def _theme_name(self) -> str:
        try:
            value = self._theme() if self._theme else None
            return "dark" if value == "dark" else "light"
        except Exception:  # noqa: BLE001
            return "light"

    def _show_locked(self, *, friend_id: str, friend_name: str, text: str) -> None:
        self._friend_id = friend_id
        snippet = text.strip()
        if len(snippet) > MAX_TEXT:
            snippet = snippet[:MAX_TEXT] + "…"
        initial = (friend_name.strip()[:1] or "?").upper()
        page = card_html(
            name=friend_name,
            text=snippet,
            initial=initial,
            dark=self._theme_name() == "dark",
        )

        if self._window is None:
            self._create_window(page)
            # ⚠️ 第一次必须**等页面加载完**再露出来：WebView2 的合成器在加载完成前
            # 接不上，这时候显示就是一片白（实测：显示后 1s / 3s 都全白，
            # 手动 hide→show 一次立刻正常）。
            if not self._loaded.wait(3.0):
                logger.warning("弹窗页面 3 秒内没加载完，先按现状显示")
        else:
            self._window.load_html(page)
        # 摆位置 + 露出来（**全程 Win32**：pywebview 的 show() 会抢焦点）
        self._place()
        if self._timer is not None:
            self._timer.cancel()
        self._timer = threading.Timer(SHOW_SECONDS, self.hide)
        self._timer.daemon = True
        self._timer.start()

    def _on_loaded(self) -> None:
        """页面加载完成 —— 到这时 WebView2 才真的能画出东西。"""
        self._loaded.set()

    def _create_window(self, page: str) -> None:
        import webview

        theme = self._theme_name()
        self._window = webview.create_window(
            TITLE,
            html=page,
            width=WIDTH,
            height=HEIGHT,
            frameless=True,
            on_top=True,
            # ⚠️ 千万别用 transparent=True：它只让 WebView 透明，
            # 窗口底色仍是 WinForms 的浅灰 → 卡片四周露出一圈白边（实测像素 E8E8E9）。
            # 窗口保持不透明，圆角交给 DWM：shadow=True 会给系统阴影。
            transparent=False,
            shadow=True,
            background_color=CARD_BG[theme],
            focus=False,
            resizable=False,
            min_size=(1, 1),
            js_api=_Api(self),
        )
        self._window.events.loaded += self._on_loaded
        # ⚠️ 尺寸必须这样做一次：create 那条路算出来的内容区不是我们要的
        #（150% 缩放实测：要 372x104，给成了 358x67）
        try:
            self._window.resize(WIDTH, HEIGHT)
        except Exception:  # noqa: BLE001
            logger.opt(exception=True).warning("弹窗尺寸校准失败（继续用默认尺寸）")
        self._hwnd = win32.wait_hwnd(TITLE)
        if not self._hwnd:
            logger.warning("弹窗窗口句柄没等到，这次不弹（不影响消息）")
            return
        win32.round_corners(self._hwnd)
        win32.drop_taskbar_button(self._hwnd)
        # hidden=True 实测不生效，自己兜一次（免得刚建出来就在屏幕上闪一下）
        self.hide()

    def _place(self) -> None:
        """摆到任务栏上方的右下角，然后**不抢焦点**地露出来。

        **为什么要在运行时自己量**：pywebview 的 `move()` 坐标、`GetWindowRect()`
        的坐标、`SystemParametersInfo` 的坐标，在 150% 缩放的机器上实测是**三套不同的单位**
        （同一个窗口能算出 x=1317 / x=1975 / x=2170 / x=3255 四种结果）。
        与其赌哪套对，不如量出来：

          1. 挪到 (x0, y0) 量一次窗口矩形；
          2. 再挪 (+200, +200) 量一次；
          3. 两点的斜率 = 逻辑坐标 → 窗口矩形的换算比例；
          4. 用这个比例反推"右下角该在哪个逻辑坐标"。

        全程在**隐藏状态下**做（挪动不可见），最后才露出来。
        """
        try:
            self._window.resize(WIDTH, HEIGHT)
        except Exception:  # noqa: BLE001
            pass
        if not self._hwnd:
            self._hwnd = win32.wait_hwnd(TITLE, 1.0)
        if not self._hwnd:
            return

        step = 200.0
        x0, y0 = 1000, 500
        try:
            self._window.move(x0, y0)
            time.sleep(0.12)
            left1, top1, _, _ = win32.window_rect(self._hwnd)
            self._window.move(int(x0 + step), int(y0 + step))
            time.sleep(0.12)
            left2, top2, right2, bottom2 = win32.window_rect(self._hwnd)
        except Exception:  # noqa: BLE001
            logger.opt(exception=True).warning("弹窗定位失败（继续弹）")
            win32.show_no_activate(self._hwnd)
            return

        scale_x = (left2 - left1) / step if left2 != left1 else 1.0
        scale_y = (top2 - top1) / step if top2 != top1 else 1.0
        width = right2 - left2
        height = bottom2 - top2
        anchor = None
        if self._anchor is not None:
            try:
                anchor = self._anchor()
            except Exception:  # noqa: BLE001 —— 问不到就退回右下角
                anchor = None
        if anchor:
            # D119：桌宠开着 → 弹在它正上方居中（越界会自己夹回工作区）
            target_x, target_y = compute_anchor_target(
                popup_size=(width, height), anchor=anchor
            )
        else:
            _, _, work_right, work_bottom = win32.work_area_of(self._hwnd)
            margin_x = int(MARGIN * scale_x)
            margin_y = int(MARGIN * scale_y)
            target_x = work_right - width - margin_x
            target_y = work_bottom - height - margin_y
        final_x = int(x0 + step + (target_x - left2) / scale_x)
        final_y = int(y0 + step + (target_y - top2) / scale_y)
        try:
            self._window.move(final_x, final_y)
        except Exception:  # noqa: BLE001
            logger.opt(exception=True).warning("弹窗定位失败（继续弹）")
        # 露 → 藏 → 露：**不是凑数，是必须的**。
        # 只在隐藏状态下 show() 一次，WebView2 的合成器不会接上（实测一片白）；
        # 中间夹一次 hide 之后，它才真的开始画（pywebview 源码里那句
        # "no idea why this works" 说的就是同一件事）。全程不抢焦点。
        win32.show_no_activate(self._hwnd)
        win32.hide_window(self._hwnd)
        win32.show_no_activate(self._hwnd)
