"""桌宠的画面（D121）：**纯函数**，输入"要显示什么"，输出一张 RGBA 图。

为什么自己画：真透明只有"原生分层窗口"给得了（WebView2 的画面是独立合成的一层，
窗口级的抠色/裁剪都碰不到它，详见 D120 末尾）。所以立绘、她的话、按钮、菜单
全在这里用 PIL 画出来，再由 `pet_native.py` 交给 `UpdateLayeredWindow`。

这个文件**不碰 Win32**（只认 PIL），所以能直接单测：
  · `split_paragraphs` —— 跟前端 D107 同一条口径（空行分段、纯括号不算一段）
  · `compute_layout` —— 窗口里每块东西在哪儿（物理像素）
  · `hit_test` —— 鼠标点在谁身上（alpha=0 的地方由系统直接放过去，不用我们管）
  · `compose_frame` —— 画一张帧
"""

from __future__ import annotations

import re
import threading
from dataclasses import dataclass, field, replace
from functools import lru_cache
from typing import Any, Iterable

from PIL import Image, ImageDraw, ImageFont

# 基准尺寸（逻辑像素）；真正的窗口 = 基准 × 缩放档位
BASE_WIDTH = 380
BASE_HEIGHT = 560
# 自由缩放的范围（D122）：像普通窗口那样随心改大小，但不至于缩没 / 撑爆屏幕
MIN_SCALE = 0.4
# 2026-09-27（用户要求）：**放开上限**（原来 3.0× 就不给放了）。
# 留一个 10× 的保险丝：再大窗口就是几万像素，Windows 那边会直接罢工。
MAX_SCALE = 10.0

# 输入框留给**独立的小窗口**（原生 EDIT，输入法要用它），这里只算它的位置
INPUT_HEIGHT = 46
INPUT_BOTTOM_GAP = 8
# 图标按钮的边长：固定 UI 尺寸，不跟角色倍率走（2026-11-07）。
# 36 在高 DPI（ui_scale=2.0）下按钮约 72 物理像素，点起来不费劲。
ICON_BUTTON = 36

# 上面留一条带给她的话（D122：不悬停时这条带是空的、透明的）
BUBBLE_ROOM_RATIO = 0.30

FONT_REGULAR = "C:/Windows/Fonts/msyh.ttc"
FONT_BOLD = "C:/Windows/Fonts/msyhbd.ttc"

ACCENT = (194, 105, 71)  # 伴侣橙，跟前端一个色
BUBBLE_BG = (12, 12, 14, 150)
BUBBLE_LINE = (255, 255, 255, 28)
INK = (255, 255, 255, 255)
FONT_SIZE = 17.0
MOOD_SIZE = 13.0
PILL_SIZE = 13.0

MENU_WIDTH = 152
MENU_ROW_HEIGHT = 26


# ── 文字处理 ─────────────────────────────────────────────────────────
def split_paragraphs(text: str) -> list[str]:
    """按自然段切（D107）：空行分段；**只由括号组成的块**并到后面的台词上。

    跟 `frontend/src/ImmersiveView.tsx::splitParagraphs` 同一条规则。
    """
    if not text:
        return []
    out: list[str] = []
    pending: list[str] = []
    for part in text.split("\n"):
        trimmed = part.strip()
        if not trimmed:
            if pending and not only_parentheses("\n".join(pending)):
                out.append("\n".join(pending))
                pending = []
            continue
        pending.append(trimmed)
        if not only_parentheses("\n".join(pending)):
            out.append("\n".join(pending))
            pending = []
    if pending:
        out.append("\n".join(pending))
    return [item for item in out if item.strip()]


def only_parentheses(text: str) -> bool:
    """整块只有（动作/心理描写）那种括号内容 → 不算独立一段（D107）。"""
    return not re.sub(r"[（(][^）)]*[）)]", "", text).strip()


@lru_cache(maxsize=64)
def _font(size: int, bold: bool = False) -> Any:
    path = FONT_BOLD if bold else FONT_REGULAR
    try:
        return ImageFont.truetype(path, size)
    except OSError:  # 字体不在（别的机器）也不能崩
        return ImageFont.load_default(size)


@lru_cache(maxsize=512)
def _cached_line_image(text: str, size: int) -> Image.Image:
    """逐字流式刷新时，已经显示完的行不重复跑一遍中文描边排版。"""
    font = _font(size)
    width = max(1, int(font.getlength(text)) + 6)
    height = max(1, int(size * 1.9))
    image = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    ImageDraw.Draw(image).text((0, 0), text, font=font, fill=INK)
    return image


def text_width(draw: ImageDraw.ImageDraw, text: str, font: Any) -> int:
    box = draw.textbbox((0, 0), text, font=font)
    return box[2] - box[0]


@lru_cache(maxsize=8192)
def _char_width(font: Any, char: str) -> float:
    return float(font.getlength(char))


def wrap_text(draw: ImageDraw.ImageDraw, text: str, font: Any, max_width: int) -> list[str]:
    """按宽度折行（中文按字折）。"""
    lines: list[str] = []
    for raw_line in text.split("\n"):
        if not raw_line:
            lines.append("")
            continue
        current = ""
        current_width = 0.0
        for char in raw_line:
            candidate = current + char
            char_width = _char_width(font, char)
            if current and current_width + char_width > max_width:
                lines.append(current)
                current = char
                current_width = char_width
            else:
                current = candidate
                current_width += char_width
        lines.append(current)
    return lines


# ── 立绘动作（D123，照抄沉浸模式 index.css 的 keyframes）─────────────
# 每个动作 = (时长秒, [(t, (dx, dy, scale, rotate_deg)), ...])；t=0..1
MOTIONS: dict[str, tuple[float, list[tuple[float, tuple[float, float, float, float]]]]] = {
    "happy": (0.90, [(0, (0, 0, 1, 0)), (0.28, (0, -22, 1.02, 0)), (0.56, (0, 0, 0.996, 0)),
                     (0.76, (0, -7, 1.008, 0)), (1, (0, 0, 1, 0))]),
    "playful": (0.90, [(0, (0, 0, 1, 0)), (0.25, (-6, 0, 1, -3)), (0.55, (6, 0, 1, 3)),
                       (0.80, (-2, 0, 1, -1.5)), (1, (0, 0, 1, 0))]),
    "shy": (0.90, [(0, (0, 0, 1, 0)), (0.35, (0, 10, 0.985, 0)), (0.70, (0, 3, 0.995, 0)),
                   (1, (0, 0, 1, 0))]),
    "angry": (0.85, [(0, (0, 0, 1, 0)), (0.12, (-7, 0, 1, -1)), (0.28, (6, 0, 1, 1)),
                     (0.46, (-5, 0, 1, -0.7)), (0.64, (4, 0, 1, 0.5)), (0.82, (-2, 0, 1, 0)),
                     (1, (0, 0, 1, 0))]),
    "surprised": (0.80, [(0, (0, 0, 1, 0)), (0.22, (0, -14, 1.035, 0)), (0.52, (0, 2, 0.995, 0)),
                         (1, (0, 0, 1, 0))]),
    "sad": (1.00, [(0, (0, 0, 1, 0)), (0.38, (0, 12, 1, -1.2)), (0.68, (0, 6, 1, 0.8)),
                   (1, (0, 0, 1, 0))]),
    "love": (0.95, [(0, (0, 0, 1, 0)), (0.30, (0, -8, 1.015, 0)), (0.60, (0, -2, 1.004, 0)),
                    (1, (0, 0, 1, 0))]),
    "proud": (0.80, [(0, (0, 0, 1, 0)), (0.40, (0, -3, 1.025, 0)), (1, (0, 0, 1, 0))]),
    "puzzled": (0.90, [(0, (0, 0, 1, 0)), (0.35, (0, 0, 1, -4)), (0.72, (0, 0, 1, 3)),
                       (1, (0, 0, 1, 0))]),
}

MOOD_MOTIONS: list[tuple[tuple[str, ...], str]] = [
    (("兴奋", "开心", "愉快", "高兴", "惊喜", "笑", "轻笑"), "happy"),
    (("调皮", "坏笑", "狡黠"), "playful"),
    (("害羞", "羞耻", "脸红"), "shy"),
    (("生气", "愤怒", "厌恶", "不爽"), "angry"),
    (("惊讶", "吃惊", "震惊"), "surprised"),
    (("伤心", "难过", "无奈", "担心", "紧张", "慌张", "害怕"), "sad"),
    (("心动", "喜欢", "爱慕", "软", "柔软", "宠溺"), "love"),
    (("自信", "认真", "得意"), "proud"),
    (("疑惑", "困惑", "懵"), "puzzled"),
]


# 换表情时的音效（D131）：动作名 → 文件名。**跟前端 moodSound.ts 是同一张表**，
# 文件在 frontend/public/sounds/（从 LingChat 那套按情绪的音效里挑的）。
MOOD_SOUNDS: dict[str, str] = {
    "happy": "happy.wav",
    "playful": "playful.wav",
    "shy": "shy.wav",
    "angry": "angry.wav",
    "surprised": "surprised.wav",
    "sad": "sad.wav",
    "love": "love.wav",
    "proud": "proud.wav",
    "puzzled": "puzzled.wav",
}


def motion_for_mood(mood: str) -> str:
    """表情 → 动作名（跟前端 `motionClassOf` 同一张表）。"""
    for words, name in MOOD_MOTIONS:
        if any(word in mood for word in words):
            return name
    return "plain"


def motion_transform(name: str, progress: float, scale: float) -> tuple[float, float, float, float]:
    """这一刻的动作变换：`(dx, dy, 缩放, 旋转角度)`（位移按 layout 缩放）。"""
    if name not in MOTIONS:
        return (0.0, 0.0, 1.0, 0.0)
    _duration, keys = MOTIONS[name]
    t = max(0.0, min(1.0, progress))
    previous = keys[0]
    for key in keys:
        if t <= key[0]:
            span = key[0] - previous[0]
            ratio = 0.0 if span <= 0 else (t - previous[0]) / span
            a, b = previous[1], key[1]
            return (
                (a[0] + (b[0] - a[0]) * ratio) * scale,
                (a[1] + (b[1] - a[1]) * ratio) * scale,
                a[2] + (b[2] - a[2]) * ratio,
                a[3] + (b[3] - a[3]) * ratio,
            )
        previous = key
    last = keys[-1][1]
    return (last[0] * scale, last[1] * scale, last[2], last[3])


# ── 布局 ────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class Rect:
    left: int
    top: int
    right: int
    bottom: int

    @property
    def width(self) -> int:
        return self.right - self.left

    @property
    def height(self) -> int:
        return self.bottom - self.top

    def contains(self, x: int, y: int) -> bool:
        return self.left <= x < self.right and self.top <= y < self.bottom


@dataclass(frozen=True)
class Button:
    key: str
    label: str
    rect: Rect


@dataclass
class Layout:
    scale: float
    width: int
    height: int
    pad: int
    name_tag: Rect
    bubble: Rect
    portrait: Rect
    input_rect: Rect
    bubble_lines: list[str] = field(default_factory=list)
    mood: str = ""
    name: str = ""
    muted: bool = False
    locked: bool = False
    ui_scale: float = 1.0
    lock_button: Button | None = None
    toolbar: Rect | None = None
    buttons: list[Button] = field(default_factory=list)
    menu_rows: list[tuple[str, Rect]] = field(default_factory=list)
    # 2026-09-27（用户要求）：**鼠标判定只在立绘的实体上**，别把透明背景也算进来。
    # 这个矩形 = 立绘"不透明内容"的外接框（`compose_frame` 画的时候顺手算出来），
    # 没算出来时（没立绘）回退到整个 portrait 区域。
    portrait_hit: Rect | None = None


def window_size(scale: float) -> tuple[int, int]:
    """分层窗口尺寸：跟着角色倍率走，不额外设上限（2026-11-07 用户要求）。"""
    return max(1, int(BASE_WIDTH * scale)), max(1, int(BASE_HEIGHT * scale))


def compute_layout(
    *,
    scale: float,
    ui_scale: float = 1.0,
    locked: bool = False,
    name: str,
    mood: str,
    text: str,
    display_text: str | None = None,
    mute: bool,
    menu_open: bool,
    menu_values: Iterable[float] = (0.8, 1.0, 1.25, 1.5),
    measure: ImageDraw.ImageDraw | None = None,
    visible: tuple[int, int, int, int] | None = None,
) -> Layout:
    """算出每块东西在窗口里的位置（物理像素）。**纯函数**。"""
    ui = max(0.8, min(2.2, float(ui_scale or 1.0)))
    width, height = window_size(scale)
    pad = int(14 * ui)

    if measure is None:
        measure = ImageDraw.Draw(Image.new("RGBA", (8, 8)))

    # 气泡跟着 UI 走，不跟着角色缩放走。窗口再大，气泡也不会变成一面墙。
    bubble_width = min(width - 2 * pad, int(360 * ui))
    font = _font(max(11, int(FONT_SIZE * ui)))
    inner_pad = int(12 * ui)
    lines = wrap_text(measure, text, font, bubble_width - 2 * inner_pad) or [""]
    display_lines = (
        wrap_text(measure, display_text, font, bubble_width - 2 * inner_pad) or [""]
        if display_text is not None
        else lines
    )
    line_height = int(FONT_SIZE * 1.62 * ui)
    mood_height = int(MOOD_SIZE * 2.0 * ui)

    # 两块：上面留带给她的话、余下全是立绘（主体）。
    bubble_room = int(height * BUBBLE_ROOM_RATIO)
    portrait = Rect(0, bubble_room, width, height)

    bubble_height = min(
        bubble_room - int(10 * ui),
        int(18 * ui) + mood_height + len(lines) * line_height + int(14 * ui),
    )
    bubble_left = (width - bubble_width) // 2
    bubble = Rect(
        bubble_left,
        bubble_room - int(10 * ui) - bubble_height,
        bubble_left + bubble_width,
        bubble_room - int(10 * ui),
    )

    # 输入框固定尺寸，靠底部居中；不再按角色倍率放大。
    input_width = min(width - 2 * pad, int(360 * ui))
    input_height = int(INPUT_HEIGHT * ui)
    input_top = portrait.top + int(portrait.height * 0.72)
    input_left = (width - input_width) // 2
    input_rect = Rect(
        input_left,
        input_top,
        input_left + input_width,
        input_top + input_height,
    )

    # 名字牌：固定 UI 尺寸，放右上角。
    top = portrait.top + int(8 * ui)
    name_font = _font(max(10, int(PILL_SIZE * ui)), bold=True)
    name_width = text_width(measure, name, name_font) + int(30 * ui)
    name_tag = Rect(
        width - int(12 * ui) - name_width,
        top,
        width - int(12 * ui),
        top + int(22 * ui),
    )

    # D466：按钮改成「头右侧的小面板」。
    # 以前按钮贴窗口最左边、锁贴最右边；大桌宠时窗口比屏幕还大，锁会跑到
    # 屏幕外面，根本点不到。现在整套 UI 锚定在角色头部右侧，并按桌宠倍率
    # 适当放大（上限 2.2×），保证巨形桌宠上也够大、够近。
    chrome = max(ui, min(2.2, max(1.0, float(scale) * 0.36)))
    buttons: list[Button] = []
    items = [
        ("main", "主界面"),
        ("game", "游戏管理"),
        ("talk", "说话"),
        ("mute", "取消静音" if mute else "静音"),
        ("switch", "换朋友"),
        ("hide", "躲起来"),
        ("zoom_in", "放大"),
        ("zoom_out", "缩小"),
    ]
    size = int(ICON_BUTTON * chrome)
    gap = int(7 * chrome)
    col_gap = int(7 * chrome)
    panel_pad = int(8 * chrome)
    columns = 3
    rows = (len(items) + columns - 1) // columns
    panel_width = panel_pad * 2 + size * columns + col_gap * (columns - 1)
    panel_height = panel_pad * 2 + size * rows + gap * (rows - 1)
    head_right = int(width * 0.60)
    head_top = portrait.top + int(portrait.height * 0.08)
    anchor_gap = int(10 * ui)
    if visible is None:
        visible_left, visible_top = pad, portrait.top + pad
        visible_right, visible_bottom = width - pad, portrait.bottom - pad
    else:
        visible_left = max(pad, int(visible[0]) + pad)
        visible_top = max(portrait.top + pad, int(visible[1]) + pad)
        visible_right = min(width - pad, int(visible[2]) - pad)
        visible_bottom = min(portrait.bottom - pad, int(visible[3]) - pad)
    # 优先放头的右侧；右边不够就贴着窗口右边收回来，但仍在头部附近。
    left = max(
        visible_left,
        min(head_right + anchor_gap, visible_right - panel_width),
    )
    lock_size = int(48 * chrome)
    lock_left = max(
        visible_left,
        min(head_right + anchor_gap, visible_right - lock_size),
    )
    lock_top = max(visible_top, min(head_top, visible_bottom - lock_size))
    top = lock_top + lock_size + anchor_gap
    if top + panel_height > visible_bottom:
        top = max(visible_top, lock_top - panel_height - anchor_gap)
    for index, (key, title) in enumerate(items):
        column = index % columns
        row = index // columns
        x = left + panel_pad + column * (size + col_gap)
        y = top + panel_pad + row * (size + gap)
        buttons.append(
            Button(key=key, label=title, rect=Rect(x, y, x + size, y + size))
        )
    toolbar = Rect(
        left,
        top,
        left + panel_width,
        top + panel_height,
    )

    # 锁定按钮单独放头部右侧；它不参与普通按钮的显隐，永远可点。
    lock_button = Button(
        key="lock",
        label="已锁定" if locked else "锁定",
        rect=Rect(
            lock_left,
            lock_top,
            lock_left + lock_size,
            lock_top + lock_size,
        ),
    )

    menu_rows: list[tuple[str, Rect]] = []
    if menu_open:
        right = width - pad
        left = right - int(MENU_WIDTH * ui)
        row_h = int(MENU_ROW_HEIGHT * ui)
        y = bubble.bottom + int(26 * ui)
        for value in menu_values:
            menu_rows.append((f"scale:{value}", Rect(left, y, right, y + row_h)))
            y += row_h
        y += int(7 * ui)
        menu_rows.append(("exit", Rect(left, y, right, y + row_h)))

    return Layout(
        scale=scale,
        ui_scale=ui,
        locked=locked,
        width=width,
        height=height,
        pad=pad,
        name_tag=name_tag,
        bubble=bubble,
        portrait=portrait,
        input_rect=input_rect,
        bubble_lines=display_lines,
        mood=mood,
        name=name,
        muted=mute,
        lock_button=lock_button,
        toolbar=toolbar,
        buttons=buttons,
        menu_rows=menu_rows,
    )


def bubble_lines_for(layout: Layout, text: str) -> list[str]:
    """流式刷新时只重排气泡文字，不动气泡/角色的几何布局。"""
    inner_pad = int(12 * layout.ui_scale)
    width = layout.bubble.width - 2 * inner_pad
    font = _font(max(11, int(FONT_SIZE * layout.ui_scale)))
    measure = ImageDraw.Draw(Image.new("RGBA", (8, 8)))
    return wrap_text(measure, text, font, width) or [""]


def render_bubble_layer(layout: Layout) -> tuple[Image.Image, tuple[int, int]]:
    """只画气泡那一小块；返回 (图, 左上角坐标)。流式刷新时不用重画整窗。"""
    ui = layout.ui_scale
    tail = int(7 * ui)
    width = layout.bubble.width
    height = layout.bubble.height + tail + 1
    local = replace(
        layout,
        bubble=Rect(0, 0, width, layout.bubble.height),
    )
    image = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    rect = local.bubble
    draw.rounded_rectangle(
        (rect.left, rect.top, rect.right - 1, rect.bottom - 1),
        radius=max(6, int(16 * ui)),
        fill=BUBBLE_BG,
        outline=BUBBLE_LINE,
        width=max(1, int(ui)),
    )
    tail = int(7 * ui)
    cx = (rect.left + rect.right) // 2
    draw.polygon(
        [
            (cx - tail, rect.bottom - 1),
            (cx + tail, rect.bottom - 1),
            (cx, rect.bottom + tail),
        ],
        fill=BUBBLE_BG,
    )
    x = rect.left + int(12 * ui)
    y = rect.top + int(7 * ui)
    if local.mood:
        draw.text(
            (x, y),
            local.mood,
            font=_font(max(9, int(MOOD_SIZE * ui)), bold=True),
            fill=ACCENT + (255,),
        )
    size = max(10, int(FONT_SIZE * ui))
    text_y = y + int(MOOD_SIZE * 2.0 * ui)
    line_height = int(FONT_SIZE * 1.65 * ui)
    for line in local.bubble_lines:
        if text_y + line_height > rect.bottom - int(4 * ui):
            break
        line_image = _cached_line_image(line, size)
        image.paste(line_image, (x, text_y), line_image)
        text_y += line_height
    return image, (layout.bubble.left, layout.bubble.top)


def chrome_hit(layout: Layout, x: int, y: int, *, pad: int = 0) -> bool:
    """"悬浮要显示/保持"的那几块：立绘实体 + 名字牌 + 图标按钮。

    2026-09-27（用户要求）：以前两处判定各写一套（鼠标消息那路用"立绘实体框"、
    定时器兜底那路用"整块立绘区"）→ 结论不一致 → 鼠标在立绘透明边那带来回横跳，
    图标按钮"闪呀闪"、还点不到。统一成这一个函数。

    另外（用户第二次反馈"按钮还是闪"）：名字牌 / 按钮给 `pad` 一点余量（够到就算），
    而且**按钮那一整列都算**——鼠标落在按钮之间的缝里也不会让它们消失。
    """
    lock = layout.lock_button
    if lock is not None and (
        lock.rect.left - pad <= x <= lock.rect.right + pad
        and lock.rect.top - pad <= y <= lock.rect.bottom + pad
    ):
        return True
    if layout.locked:
        return False

    toolbar = layout.toolbar
    if toolbar is not None and (
        toolbar.left - pad <= x <= toolbar.right + pad
        and toolbar.top - pad <= y <= toolbar.bottom + pad
    ):
        return True
    area = layout.portrait_hit or layout.portrait
    if area.contains(x, y):
        return True

    def near(rect: Rect) -> bool:
        return near_pad(rect, pad)

    def near_pad(rect: Rect, amount: int) -> bool:
        return (
            rect.left - amount <= x <= rect.right + amount
            and rect.top - amount <= y <= rect.bottom + amount
        )

    if near(layout.name_tag):
        return True
    if not layout.buttons:
        return False
    # 竖排那一列的**外接框**（含按钮之间的缝）
    column = Rect(
        min(button.rect.left for button in layout.buttons),
        min(button.rect.top for button in layout.buttons),
        max(button.rect.right for button in layout.buttons),
        max(button.rect.bottom for button in layout.buttons),
    )
    # 2026-10-03（用户实测"按钮挪到左边就点不到了"）：按钮那一列**在立绘实体外面**，
    # 从立绘走到按钮要跨一段"透明区"——那段收不到鼠标消息，余量太小就会被判"出去了"
    # → 按钮一闪、还点不到。所以这一列单独给**更大**的余量。
    return near_pad(column, pad + max(6, int(10 * layout.scale)))


def hit_test(
    layout: Layout,
    x: int,
    y: int,
    *,
    hover_buttons: bool,
    menu_open: bool,
) -> str | None:
    """鼠标落在谁身上 → 一个动作名（None = 空白；空白处系统会直接放过去）。"""
    if menu_open:
        for key, rect in layout.menu_rows:
            if rect.contains(x, y):
                return f"menu:{key}"
        return "menu:close"
    lock = layout.lock_button
    if lock is not None:
        # 视觉上按钮只有 48×48，但给一圈额外命中区；大桌宠上鼠标稍微偏一点
        # 也能点中（用户反馈"锁按钮点不到"）。
        pad = max(6, int(8 * layout.ui_scale), int(lock.rect.width * 0.12))
        if (
            lock.rect.left - pad <= x <= lock.rect.right + pad
            and lock.rect.top - pad <= y <= lock.rect.bottom + pad
        ):
            return "button:lock"
    # 锁定后只有锁定按钮可点；立绘、名字牌、菜单、普通按钮全部失效。
    if layout.locked:
        return None
    # 名字牌 / 图标按钮**悬浮才画** → 没悬浮的时候别认它们（不然会点到看不见的东西）
    if hover_buttons:
        for button in layout.buttons:
            if button.rect.contains(x, y):
                return f"button:{button.key}"
        if layout.name_tag.contains(x, y):
            return "drag"  # 名字牌也能拖着走（按住它 = 拖窗口）
    # ⚠️ 气泡**不是点击目标**（用户 2026-09-27："聊天框不该在点击范围里"）：
    # 点它就等于没点到；外壳还会把"只有气泡"的那块设成鼠标穿透（见 WM_NCHITTEST）。
    # 立绘：只认**实体**（不透明内容的外接框），透明边不算 —— 用户 2026-09-27 的要求
    portrait_area = layout.portrait_hit or layout.portrait
    if portrait_area.contains(x, y):
        return "advance"
    return None


# ── 画 ──────────────────────────────────────────────────────────────
class TransformCache:
    """动作变换（缩放 / 旋转）结果的缓存（2026-09-27，D213）。

    **为什么需要**：立绘是按窗口缩放好的大图（用户这台机器 723×964），每帧
    `resize` ≈ 9ms、`rotate` ≈ 19ms —— 动作动画要 30fps，光变换就超预算，
    表现出来就是"换动作表情特别卡"。实测：

        只贴图（无变换） 5.5ms ／ 带旋转 24.9ms ／ 另外 `_blit` 还要 12ms

    动作是**固定几条关键帧**插值出来的，同一套动作第二次开始参数几乎重合
    （按 0.5% 尺寸 + 0.5° 角度量化后只有十几帧）→ 缓存下来，第二遍几乎零成本。

    键里带 `id(image)`，但条目里**同时存着源图的引用**并在命中时用 `is` 比对，
    所以 `id` 被回收复用也不会串图。按字节数做 LRU 上限，大倍率下也不会爆内存。
    """

    def __init__(self, budget_bytes: int = 48 * 1024 * 1024) -> None:
        self._budget = int(budget_bytes)
        self._items: dict[tuple[int, float, float], tuple[Image.Image, Image.Image]] = {}
        self._order: list[tuple[int, float, float]] = []
        self._bytes = 0
        self._lock = threading.Lock()

    @staticmethod
    def _size_of(image: Image.Image) -> int:
        return image.width * image.height * 4

    def get(self, key: tuple[int, float, float], source: Image.Image) -> Image.Image | None:
        with self._lock:
            item = self._items.get(key)
            if item is None or item[0] is not source:
                return None
            self._touch(key)
            return item[1]

    def put(
        self,
        key: tuple[int, float, float],
        source: Image.Image,
        image: Image.Image,
    ) -> Image.Image:
        size = self._size_of(image)
        if size > self._budget:  # 单张就超预算（超大倍率）→ 不缓存，别撑爆内存
            return image
        with self._lock:
            existing = self._items.pop(key, None)
            if existing is not None:
                self._bytes -= self._size_of(existing[1])
                self._discard(key)
            self._items[key] = (source, image)
            self._order.append(key)
            self._bytes += size
            while self._bytes > self._budget and len(self._order) > 1:
                stale = self._order.pop(0)
                dropped = self._items.pop(stale, None)
                if dropped is not None:
                    self._bytes -= self._size_of(dropped[1])
        return image

    def clear(self) -> None:
        with self._lock:
            self._items.clear()
            self._order.clear()
            self._bytes = 0

    # 下面两个只在持锁时调
    def _touch(self, key: tuple[int, float, float]) -> None:
        self._discard(key)
        self._order.append(key)

    def _discard(self, key: tuple[int, float, float]) -> None:
        try:
            self._order.remove(key)
        except ValueError:
            pass


def quantize_motion(motion_scale: float, rotate: float) -> tuple[float, float]:
    """把动作参数量化成缓存键（尺寸 0.5%，角度 0.5°）。"""
    scale_q = round(motion_scale / 0.005) * 0.005
    rotate_q = round(rotate / 0.5) * 0.5
    return round(scale_q, 4), round(rotate_q, 4)


def transform_motion(
    image: Image.Image,
    motion_scale: float,
    rotate: float,
    cache: TransformCache | None = None,
) -> Image.Image:
    """按动作把立绘缩放 / 旋转好（`cache` 给了就复用，2026-09-27 D213）。

    旋转用 `expand=False`：**保持尺寸不变**。`expand=True` 会让图变大、然后被
    `compose_frame` 的"太大了兜底缩回去"再缩一次 —— 每帧多花 10ms 且尺寸抖动。
    同一张图需要同一套变换时，第二次直接拿缓存。
    """
    scale_q, rotate_q = quantize_motion(motion_scale, rotate)
    plain = abs(scale_q - 1.0) < 0.001 and abs(rotate_q) < 0.5
    if plain:
        return image
    key = (id(image), scale_q, rotate_q)
    if cache is not None:
        hit = cache.get(key, image)
        if hit is not None:
            return hit
    source = image
    if abs(scale_q - 1.0) >= 0.001:
        image = image.resize(
            (
                max(1, int(image.width * scale_q)),
                max(1, int(image.height * scale_q)),
            ),
            Image.BILINEAR,
        )
    if abs(rotate_q) >= 0.5:
        image = image.rotate(rotate_q, resample=Image.BILINEAR, expand=False)
    if cache is not None:
        cache.put(key, source, image)
    return image


def compose_frame(
    *,
    layout: Layout,
    portrait: Image.Image | None,
    show_buttons: bool,
    show_bubble: bool,
    menu_open: bool,
    active_scale: float,
    motion: tuple[float, float, float, float] | None = None,
    transform_cache: TransformCache | None = None,
    chrome: Image.Image | None = None,
) -> Image.Image:
    """画一帧（RGBA）。**透明的地方 alpha=0** —— 那才是"只有立绘"。"""
    frame = Image.new("RGBA", (layout.width, layout.height), (0, 0, 0, 0))

    if portrait is not None:
        area = layout.portrait
        # 2026-09-27：已经是 RGBA 就别 copy（每帧都 copy 一份立绘 → "卡"的一个来源）
        image = portrait if portrait.mode == "RGBA" else portrait.convert("RGBA")
        if image.width and image.height and area.width > 0 and area.height > 0:
            dx, dy = 0.0, 0.0
            if motion is not None:
                # 立绘动作（D123）：位移 / 缩放 / 旋转，都绕"底边中点"
                dx, dy, motion_scale, rotate = motion
                # 2026-09-27（D213）：变换走缓存 + expand=False；动作帧**故意**会比
                # 区域大一点点（1%~3.5% 的"呼吸"），所以这里不再"兜底缩回去"。
                image = transform_motion(image, motion_scale, rotate, transform_cache)
            # 传进来的立绘一般**已经按区域缩好了**（外壳按尺寸缓存，D123）；
            # 只有**静止且**比区域还大时才在这儿兜一次底。
            elif image.width > area.width or image.height > area.height:
                ratio = min(area.width / image.width, area.height / image.height)
                image = image.resize(
                    (max(1, int(image.width * ratio)), max(1, int(image.height * ratio))),
                    Image.BILINEAR,
                )
            left = area.left + (area.width - image.width) // 2 + int(dx)
            top = area.bottom - image.height + int(dy)
            frame.alpha_composite(image, (left, top))
            # 顺手记下"立绘实体"的外接框（alpha 非零的部分）——鼠标判定只认它，
            # 这样立绘周围的透明边就不会触发悬浮（用户 2026-09-27 报的）。
            try:
                box = image.getchannel("A").getbbox()
            except Exception:  # noqa: BLE001 —— 算不出来就退回整块区域
                box = None
            if box:
                layout.portrait_hit = Rect(
                    left + box[0], top + box[1], left + box[2], top + box[3]
                )

    overlay = chrome
    if overlay is None:
        overlay = render_chrome(
            layout=layout,
            show_buttons=show_buttons,
            show_bubble=show_bubble,
            menu_open=menu_open,
            active_scale=active_scale,
        )
    frame.alpha_composite(overlay)
    return frame


def render_chrome(
    *,
    layout: Layout,
    show_buttons: bool,
    show_bubble: bool,
    menu_open: bool,
    active_scale: float,
) -> Image.Image:
    """只画 UI 层（气泡 / 名字牌 / 按钮 / 菜单 / 锁定），不碰立绘。

    桌宠窗口的 UI 在多数帧里是不变的；把它缓存成一张透明覆盖层，每帧只需要
    `alpha_composite` 一次，不用重新排版文字、画图标。
    """
    overlay = Image.new("RGBA", (layout.width, layout.height), (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)
    ui = layout.ui_scale
    # 2026-09-27（用户要求）：名字牌 + 按钮**悬浮才显示**（跟那排按钮一个节奏），
    # 平时藏起来，画面更干净。
    if not layout.locked:
        if show_buttons:
            _draw_name_tag(draw, layout, ui)
        if show_bubble:
            _draw_bubble(draw, layout, ui)
        if show_buttons:
            _draw_buttons(draw, layout, ui)
        _draw_lock_button(draw, layout)
        if menu_open:
            _draw_menu(draw, layout, ui, active_scale)
    else:
        _draw_lock_button(draw, layout)
    return overlay


def _draw_name_tag(draw: ImageDraw.ImageDraw, layout: Layout, scale: float) -> None:
    """名字牌：**立绘右下角**的小胶囊（D122），一直在。"""
    rect = layout.name_tag
    draw.rounded_rectangle(
        (rect.left, rect.top, rect.right - 1, rect.bottom - 1),
        radius=rect.height // 2,
        fill=(12, 12, 14, 120),
        outline=BUBBLE_LINE,
        width=max(1, int(scale)),
    )
    dot_r = max(2, int(3 * scale))
    dot_x = rect.left + int(9 * scale)
    dot_y = (rect.top + rect.bottom) // 2
    draw.ellipse((dot_x - dot_r, dot_y - dot_r, dot_x + dot_r, dot_y + dot_r), fill=ACCENT)
    font = _font(max(9, int(PILL_SIZE * scale)), bold=True)
    draw.text(
        (dot_x + dot_r + int(5 * scale), dot_y),
        layout.name,
        font=font,
        fill=(255, 255, 255, 235),
        anchor="lm",
        stroke_width=max(1, int(scale)),
        stroke_fill=(0, 0, 0, 160),
    )


def _draw_bubble(draw: ImageDraw.ImageDraw, layout: Layout, scale: float) -> None:
    rect = layout.bubble
    draw.rounded_rectangle(
        (rect.left, rect.top, rect.right - 1, rect.bottom - 1),
        radius=max(6, int(16 * scale)),
        fill=BUBBLE_BG,
        outline=BUBBLE_LINE,
        width=max(1, int(scale)),
    )
    tail = int(7 * scale)
    cx = (rect.left + rect.right) // 2
    draw.polygon(
        [
            (cx - tail, rect.bottom - 1),
            (cx + tail, rect.bottom - 1),
            (cx, rect.bottom + tail),
        ],
        fill=BUBBLE_BG,
    )
    x = rect.left + int(12 * scale)
    y = rect.top + int(7 * scale)
    if layout.mood:
        draw.text(
            (x, y),
            layout.mood,
            font=_font(max(9, int(MOOD_SIZE * scale)), bold=True),
            fill=ACCENT + (255,),
            stroke_width=max(1, int(scale)),
            stroke_fill=(0, 0, 0, 150),
        )
    font = _font(max(10, int(FONT_SIZE * scale)))
    text_y = y + int(MOOD_SIZE * 2.0 * scale)
    line_height = int(FONT_SIZE * 1.65 * scale)
    for line in layout.bubble_lines:
        if text_y + line_height > rect.bottom - int(4 * scale):
            break
        draw.text(
            (x, text_y),
            line,
            font=font,
            fill=INK,
        )
        text_y += line_height


def _draw_buttons(draw: ImageDraw.ImageDraw, layout: Layout, scale: float) -> None:
    """两列图标面板（2026-11-07）：**不写字**，每个键画一个小矢量图标。

    面板本身是一整块深色玻璃，按钮只在里面排两列；比原来八个独立方块更安静，
    也不会因为角色放大就跟着膨胀、把画面挡掉一半。
    """
    toolbar = layout.toolbar
    if toolbar is not None:
        draw.rounded_rectangle(
            (toolbar.left, toolbar.top, toolbar.right - 1, toolbar.bottom - 1),
            radius=max(8, int(14 * scale)),
            fill=(13, 15, 19, 172),
            outline=(255, 255, 255, 30),
            width=max(1, int(scale)),
        )
        # 顶部一条很淡的高光，让深色面板不显得像一块死黑
        draw.rounded_rectangle(
            (toolbar.left + int(3 * scale), toolbar.top + int(2 * scale),
             toolbar.right - int(3 * scale), toolbar.top + int(9 * scale)),
            radius=max(4, int(6 * scale)),
            fill=(255, 255, 255, 8),
        )
    for button in layout.buttons:
        rect = button.rect
        draw.rounded_rectangle(
            (rect.left, rect.top, rect.right - 1, rect.bottom - 1),
            radius=max(5, int(9 * scale)),
            fill=(255, 255, 255, 10),
            outline=(255, 255, 255, 16),
            width=max(1, int(0.8 * scale)),
        )
        _draw_icon(draw, button.key, rect, scale, muted=layout.muted)


def _draw_lock_button(draw: ImageDraw.ImageDraw, layout: Layout) -> None:
    button = layout.lock_button
    if button is None:
        return
    rect = button.rect
    ui = layout.ui_scale
    fill = (194, 105, 71, 235) if layout.locked else (13, 15, 19, 188)
    outline = (255, 255, 255, 105) if layout.locked else (255, 255, 255, 34)
    draw.rounded_rectangle(
        (rect.left, rect.top, rect.right - 1, rect.bottom - 1),
        radius=max(7, int(13 * ui)),
        fill=fill,
        outline=outline,
        width=max(1, int(1.2 * ui)),
    )
    if layout.locked:
        # 锁定时加一圈很淡的外环，提示“这不只是普通按钮”
        draw.rounded_rectangle(
            (rect.left - int(3 * ui), rect.top - int(3 * ui),
             rect.right + int(3 * ui), rect.bottom + int(3 * ui)),
            radius=max(9, int(17 * ui)),
            outline=(194, 105, 71, 90),
            width=max(1, int(1.4 * ui)),
        )
    _draw_icon(draw, "lock", rect, ui, locked=layout.locked)


def _draw_icon(
    draw: ImageDraw.ImageDraw,
    key: str,
    rect: Rect,
    scale: float,
    *,
    muted: bool = False,
    locked: bool = False,
) -> None:
    """把 `key` 画成一个**小图标**（纯图形，不用字体/emoji）。"""
    cx = (rect.left + rect.right) // 2
    cy = (rect.top + rect.bottom) // 2
    r = max(5, int(min(rect.width, rect.height) * 0.30))
    w = max(1, int(round(1.5 * scale)))
    ink = (255, 255, 255, 235)

    def line(x1: float, y1: float, x2: float, y2: float) -> None:
        draw.line((int(x1), int(y1), int(x2), int(y2)), fill=ink, width=w)

    if key == "main":
        # 窗口：外框 + 上面一条标题栏
        draw.rounded_rectangle(
            (cx - r, cy - int(r * 0.85), cx + r, cy + int(r * 0.85)),
            radius=max(2, int(2 * scale)),
            outline=ink,
            width=w,
        )
        line(cx - r, cy - int(r * 0.3), cx + r, cy - int(r * 0.3))
    elif key == "talk":
        # 气泡 + 小尾巴
        draw.rounded_rectangle(
            (cx - r, cy - int(r * 0.8), cx + int(r * 0.85), cy + int(r * 0.45)),
            radius=max(2, int(3.5 * scale)),
            outline=ink,
            width=w,
        )
        draw.polygon(
            [
                (cx - int(r * 0.45), cy + int(r * 0.45)),
                (cx - int(r * 0.05), cy + int(r * 0.45)),
                (cx - int(r * 0.45), cy + int(r * 0.95)),
            ],
            fill=ink,
        )
    elif key == "game":
        # 游戏手柄：圆角横框 + 左侧十字 + 右侧两个按键
        draw.rounded_rectangle(
            (cx - int(r * 1.05), cy - int(r * 0.58), cx + int(r * 1.05), cy + int(r * 0.58)),
            radius=max(2, int(4 * scale)),
            outline=ink,
            width=w,
        )
        line(cx - int(r * 0.5), cy - int(r * 0.58), cx - int(r * 0.3), cy - int(r * 1.0))
        line(cx + int(r * 0.5), cy - int(r * 0.58), cx + int(r * 0.3), cy - int(r * 1.0))
        line(cx - int(r * 0.55), cy - int(r * 0.18), cx - int(r * 0.55), cy + int(r * 0.18))
        line(cx - int(r * 0.73), cy, cx - int(r * 0.37), cy)
        draw.ellipse(
            (cx + int(r * 0.28), cy - int(r * 0.28), cx + int(r * 0.5), cy - int(r * 0.06)),
            outline=ink,
            width=w,
        )
        draw.ellipse(
            (cx + int(r * 0.52), cy + int(r * 0.04), cx + int(r * 0.74), cy + int(r * 0.26)),
            outline=ink,
            width=w,
        )
    elif key == "lock":
        # 锁：锁梁 + 锁体；打开状态把锁梁向右上偏一点。
        body_top = cy - int(r * 0.05)
        body_bottom = cy + int(r * 0.82)
        draw.rounded_rectangle(
            (cx - int(r * 0.72), body_top, cx + int(r * 0.72), body_bottom),
            radius=max(2, int(3.5 * scale)),
            outline=ink,
            width=w,
        )
        if locked:
            draw.arc(
                (cx - int(r * 0.48), cy - int(r * 0.95), cx + int(r * 0.48), cy + int(r * 0.22)),
                start=180,
                end=360,
                fill=ink,
                width=w,
            )
        else:
            draw.arc(
                (cx - int(r * 0.15), cy - int(r * 0.95), cx + int(r * 0.82), cy + int(r * 0.05)),
                start=185,
                end=345,
                fill=ink,
                width=w,
            )
        draw.ellipse(
            (cx - int(r * 0.12), cy + int(r * 0.20), cx + int(r * 0.12), cy + int(r * 0.44)),
            fill=ink,
        )
    elif key == "mute":
        # 喇叭（一个多边形）；静音时加一条斜杠，否则画声波
        draw.polygon(
            [
                (cx - int(r * 0.85), cy - int(r * 0.3)),
                (cx - int(r * 0.3), cy - int(r * 0.3)),
                (cx + int(r * 0.2), cy - int(r * 0.8)),
                (cx + int(r * 0.2), cy + int(r * 0.8)),
                (cx - int(r * 0.3), cy + int(r * 0.3)),
                (cx - int(r * 0.85), cy + int(r * 0.3)),
            ],
            fill=ink,
        )
        if muted:
            line(cx - int(r * 0.1), cy + int(r * 0.1), cx + r, cy - r)
            line(cx + r, cy + int(r * 0.3), cx + int(r * 1.1), cy)  # 尾巴，别看着像"没画完"
        else:
            draw.arc(
                (cx + int(r * 0.05), cy - int(r * 0.55), cx + int(r * 1.15), cy + int(r * 0.55)),
                start=-55, end=55, fill=ink, width=w,
            )
    elif key == "switch":
        # 双向箭头（换朋友）
        line(cx - r, cy - int(r * 0.4), cx + r, cy - int(r * 0.4))
        draw.polygon(
            [
                (cx + r, cy - int(r * 0.4)),
                (cx + int(r * 0.35), cy - int(r * 0.85)),
                (cx + int(r * 0.35), cy + int(r * 0.05)),
            ],
            fill=ink,
        )
        line(cx - r, cy + int(r * 0.4), cx + r, cy + int(r * 0.4))
        draw.polygon(
            [
                (cx - r, cy + int(r * 0.4)),
                (cx - int(r * 0.35), cy - int(r * 0.05)),
                (cx - int(r * 0.35), cy + int(r * 0.85)),
            ],
            fill=ink,
        )
    elif key == "hide":
        # 向下箭头（躲起来）
        line(cx, cy - int(r * 0.85), cx, cy + int(r * 0.45))
        draw.polygon(
            [
                (cx, cy + r),
                (cx - int(r * 0.55), cy + int(r * 0.2)),
                (cx + int(r * 0.55), cy + int(r * 0.2)),
            ],
            fill=ink,
        )
    elif key in ("zoom_in", "zoom_out"):
        # 放大镜：圆 + 手柄 + 里面 +/-（**只看图形就能分出来**）
        rr = int(r * 0.72)
        mx, my = cx - int(r * 0.22), cy - int(r * 0.22)
        draw.ellipse((mx - rr, my - rr, mx + rr, my + rr), outline=ink, width=w)
        line(mx + int(rr * 0.72), my + int(rr * 0.72), mx + rr + int(r * 0.5), my + rr + int(r * 0.5))
        line(mx - int(rr * 0.45), my, mx + int(rr * 0.45), my)
        if key == "zoom_in":
            line(mx, my - int(rr * 0.45), mx, my + int(rr * 0.45))


def _draw_menu(
    draw: ImageDraw.ImageDraw, layout: Layout, scale: float, active_scale: float
) -> None:
    if not layout.menu_rows:
        return
    left = min(rect.left for _, rect in layout.menu_rows) - int(6 * scale)
    right = max(rect.right for _, rect in layout.menu_rows) + int(6 * scale)
    top = layout.menu_rows[0][1].top - int(24 * scale)
    bottom = layout.menu_rows[-1][1].bottom + int(6 * scale)
    draw.rounded_rectangle(
        (left, top, right, bottom),
        radius=max(6, int(12 * scale)),
        fill=(28, 24, 20, 245),
        outline=(255, 255, 255, 30),
        width=max(1, int(scale)),
    )
    draw.text(
        (left + int(10 * scale), top + int(9 * scale)),
        "大小",
        font=_font(max(9, int(11 * scale))),
        fill=(255, 255, 255, 130),
    )
    font = _font(max(9, int(12.5 * scale)))
    for key, rect in layout.menu_rows:
        if key == "exit":
            text, color = "退出桌宠模式", (255, 255, 255, 220)
        else:
            value = float(key.split(":", 1)[1])
            text = f"{round(value * 100)}%"
            color = ACCENT + (255,) if abs(value - active_scale) < 0.001 else (255, 255, 255, 215)
        draw.text(
            (rect.left + int(10 * scale), (rect.top + rect.bottom) // 2),
            text,
            font=font,
            fill=color,
            anchor="lm",
        )
