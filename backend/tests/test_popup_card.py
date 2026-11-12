"""桌面弹窗的卡片 HTML（D70）——纯函数，好测。

这里最要紧的一条是**转义**：朋友说的话可能是任意文本
（用户自己写的人格、模型编出来的怪句子），塞进 HTML 之前必须转义，
不然 `<script>` 之类的东西会真的跑起来。
"""

from __future__ import annotations

from backend.desktop.popup import MAX_TEXT, card_html


def _page(**kw):
    base = {"name": "小敏", "text": "你好", "initial": "小", "dark": False}
    base.update(kw)
    return card_html(**base)


def test_message_text_is_escaped():
    page = _page(text="<script>alert(1)</script>")

    assert "<script>alert(1)</script>" not in page
    assert "&lt;script&gt;" in page


def test_name_and_initial_are_escaped():
    page = _page(name='<img src=x onerror="x">', initial="<")

    assert "<img" not in page
    assert "&lt;img" in page


def test_dark_and_light_use_different_palette():
    light = _page(dark=False)
    dark = _page(dark=True)

    assert light != dark
    assert "#0f172a" in light  # 浅色：正文是深色
    assert "#f3ece2" in dark  # 深色：正文是暖白


def test_card_is_clickable_and_topmost_ready():
    page = _page()

    assert "open_friend" in page  # 点一下要能回调 Python
    assert "pointer" in page


def test_page_background_is_opaque_and_matches_window():
    """回归：页面底色必须**不透明**，而且和窗口底色一致。

    第一版用了 `background:transparent` + CSS 圆角，结果窗口底色（WinForms 默认浅灰
    #F0F0F0）在卡片四周露出来，变成一圈白边（实拍像素 E8E8E9）。
    现在窗口不透明、圆角交给 DWM，页面底色必须跟着窗口底色走。
    """
    from backend.desktop.popup import CARD_BG

    assert "transparent" not in _page(dark=False)
    assert "transparent" not in _page(dark=True)
    assert CARD_BG["light"] in _page(dark=False)
    assert CARD_BG["dark"] in _page(dark=True)
    assert "box-shadow" not in _page()  # 阴影交给 DWM，别自己画（画了也看不见）
    assert "border-radius:16px" not in _page()  # 圆角交给 DWM


def test_max_text_constant_is_sane():
    # 卡片只显示两行，太长的文本应该在 Python 侧先拦住（别传一个几万字的 payload）
    assert 0 < MAX_TEXT <= 500
