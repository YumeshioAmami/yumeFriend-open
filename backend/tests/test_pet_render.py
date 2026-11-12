"""桌宠的画面（D121）：纯函数，离线可测。

盯三件事：
  · 分段口径跟前端一致（D107：空行分段、纯括号不算一段）
  · 布局 / 命中测试的数学（点在谁身上）
  · **透明**：空白处 alpha 必须是 0（那才是"只有立绘"），气泡/文字处必须不透明
"""

from __future__ import annotations

from PIL import Image, ImageDraw

from backend.desktop import pet_render as R
from backend.desktop.pet_native import premultiplied_bgra


def test_split_paragraphs_follows_d107():
    text = "（抬起头，尾巴晃了一下）\n\n还行呀，刚睡醒。\n\n**担心**：喝过水没有？"
    # 第一块只有动作 → 并到后面的台词上（D107 的口径）
    assert R.split_paragraphs(text) == [
        "（抬起头，尾巴晃了一下）\n还行呀，刚睡醒。",
        "**担心**：喝过水没有？",
    ]


def test_split_paragraphs_merges_lone_parenthesis_lines():
    """只有动作的那一行要并到后面的台词上（跟前端 splitParagraphs 同一条）。"""
    text = "（凑过来看了一眼）\n（又缩回去）\n\n今天挺冷的。"
    assert R.split_paragraphs(text) == ["（凑过来看了一眼）\n（又缩回去）\n今天挺冷的。"]
    text2 = "（点头）好呀。\n\n（笑）那我去啦。"
    assert R.split_paragraphs(text2) == ["（点头）好呀。", "（笑）那我去啦。"]


def test_split_paragraphs_handles_empty():
    assert R.split_paragraphs("") == []
    assert R.split_paragraphs("   ") == []


def test_only_parentheses():
    assert R.only_parentheses("（心里想着你）") is True
    assert R.only_parentheses("(thinking)") is True
    assert R.only_parentheses("（点头）好呀") is False


def test_layout_places_things_in_order():
    """D122 的口径：上面留带给她的话 → 立绘（主体）→ 输入贴着立绘底边。"""
    layout = R.compute_layout(
        scale=1.0, name="阿月", mood="高兴", text="还行呀，刚睡醒。", mute=False, menu_open=False
    )

    assert layout.bubble.bottom <= layout.portrait.top
    # （D123）输入框挪到**立绘中间偏下**（不是窗口最底下、也不是立绘外面）
    assert layout.portrait.top < layout.input_rect.top < layout.portrait.bottom
    assert layout.input_rect.bottom <= layout.portrait.bottom
    assert layout.input_rect.top > layout.portrait.top + layout.portrait.height * 0.5
    # 名字牌在**右上角**（2026-09-27：抓角删了，缩放改成图标按钮）
    assert layout.name_tag.left > layout.width // 2
    assert layout.name_tag.top >= layout.portrait.top
    assert layout.name_tag.bottom <= layout.portrait.bottom


def test_buttons_form_a_three_column_head_palette():
    """D466：按钮改成头部右侧的三列小面板，不再贴窗口最左侧。"""
    layout = R.compute_layout(
        scale=1.0, name="阿月", mood="高兴", text="还行", mute=False, menu_open=False
    )
    keys = [b.key for b in layout.buttons]
    assert keys == [
        "main",
        "game",
        "talk",
        "mute",
        "switch",
        "hide",
        "zoom_in",
        "zoom_out",
    ]
    # 三列：同列向下、第二列在同一行；宽度都一样
    for index, button in enumerate(layout.buttons):
        assert button.rect.width == layout.buttons[0].rect.width
        expected_left = layout.buttons[index % 3].rect.left
        expected_top = layout.buttons[(index // 3) * 3].rect.top
        assert button.rect.left == expected_left
        assert button.rect.top == expected_top
    assert layout.toolbar is not None
    assert all(
        layout.toolbar.left <= b.rect.left and b.rect.right <= layout.toolbar.right
        and layout.toolbar.top <= b.rect.top and b.rect.bottom <= layout.toolbar.bottom
        for b in layout.buttons
    )
    assert layout.buttons[0].rect.left >= 0
    assert layout.toolbar.left >= layout.width * 0.55
    assert layout.toolbar.right <= layout.width


def test_layout_scales_with_the_step_but_ui_stays_fixed():
    small = R.compute_layout(
        scale=0.8, name="阿月", mood="高兴", text="还行", mute=False, menu_open=False
    )
    big = R.compute_layout(
        scale=1.5, name="阿月", mood="高兴", text="还行", mute=False, menu_open=False
    )

    assert big.width > small.width
    # 角色窗口会变大，但按钮/名字牌不跟着角色倍率一起膨胀。
    assert big.buttons[0].rect.width == small.buttons[0].rect.width
    assert big.name_tag.height == small.name_tag.height


def test_lock_button_is_on_the_right_and_only_clickable_when_locked():
    layout = R.compute_layout(
        scale=1.5,
        ui_scale=1.5,
        locked=True,
        name="阿月",
        mood="高兴",
        text="还行",
        mute=False,
        menu_open=False,
    )
    assert layout.lock_button is not None
    assert layout.lock_button.rect.left > layout.width // 2
    # 锁按钮的命中区比视觉大一圈，鼠标稍微偏一点也能点到。
    left = layout.lock_button.rect.left
    top = layout.lock_button.rect.top
    assert R.hit_test(
        layout, left - 4, top + 4, hover_buttons=False, menu_open=False
    ) == "button:lock"
    assert R.hit_test(
        layout, *center(layout.lock_button.rect), hover_buttons=False, menu_open=False
    ) == "button:lock"
    assert R.hit_test(
        layout, *center(layout.portrait), hover_buttons=True, menu_open=False
    ) is None
    assert R.hit_test(
        layout, *center(layout.name_tag), hover_buttons=True, menu_open=False
    ) is None
    assert R.chrome_hit(layout, *center(layout.lock_button.rect)) is True
    assert R.chrome_hit(layout, *center(layout.portrait)) is False


def test_giant_pet_buttons_scale_and_lock_stays_near_head():
    """6× 桌宠上按钮不能还是 36px 的小点，锁也不能跑到窗口最右边。"""
    small = R.compute_layout(
        scale=1.0, name="阿月", mood="高兴", text="还行", mute=False, menu_open=False
    )
    giant = R.compute_layout(
        scale=6.22, name="阿月", mood="高兴", text="还行", mute=False, menu_open=False
    )
    assert giant.buttons[0].rect.width > small.buttons[0].rect.width * 1.5
    assert giant.lock_button is not None
    assert giant.lock_button.rect.width > small.lock_button.rect.width
    # 锁在头右侧附近（窗口宽度的 60% 左右），且完整在窗口内。
    assert abs(giant.lock_button.rect.left - giant.width * 0.60) <= giant.width * 0.05
    assert giant.lock_button.rect.right <= giant.width


def test_streaming_text_does_not_move_the_portrait_or_bubble_geometry():
    full = "这是一段很长的话，流式显示时会一个字一个字出现，但布局必须保持稳定。"
    layout_full = R.compute_layout(
        scale=1.0,
        name="阿月",
        mood="高兴",
        text=full,
        display_text=full,
        mute=False,
        menu_open=False,
    )
    layout_partial = R.compute_layout(
        scale=1.0,
        name="阿月",
        mood="高兴",
        text=full,
        display_text=full[:8],
        mute=False,
        menu_open=False,
    )

    assert layout_partial.bubble == layout_full.bubble
    assert layout_partial.portrait == layout_full.portrait
    assert layout_partial.bubble_lines == R.bubble_lines_for(layout_partial, full[:8])


def test_locked_chrome_shows_only_the_lock_button():
    layout = R.compute_layout(
        scale=1.5,
        ui_scale=1.5,
        locked=True,
        name="阿月",
        mood="高兴",
        text="还行",
        mute=False,
        menu_open=False,
    )
    chrome = R.render_chrome(
        layout=layout,
        show_buttons=True,
        show_bubble=True,
        menu_open=True,
        active_scale=1.5,
    )
    assert chrome.getpixel(center(layout.lock_button.rect))[3] > 0
    assert chrome.getpixel(center(layout.buttons[0].rect))[3] == 0
    assert chrome.getpixel(center(layout.name_tag))[3] == 0
    assert chrome.getpixel(center(layout.bubble))[3] == 0


def test_hit_test_finds_each_region():
    layout = R.compute_layout(
        scale=1.0, name="阿月", mood="高兴", text="还行", mute=False, menu_open=False
    )

    # 2026-09-27（用户要求）：**气泡不是点击目标**（点它 = 没点到；外壳还会让鼠标穿过去）
    assert R.hit_test(layout, *center(layout.bubble), hover_buttons=False, menu_open=False) is None
    # 名字牌：悬浮才画 → 不悬浮时不认它（点上去只当普通立绘点击），悬浮时 = 拖窗口
    name_point = center(layout.name_tag)
    assert R.hit_test(layout, *name_point, hover_buttons=False, menu_open=False) == "advance"
    assert R.hit_test(layout, *name_point, hover_buttons=True, menu_open=False) == "drag"
    assert R.hit_test(layout, *center(layout.portrait), hover_buttons=False, menu_open=False) == "advance"
    # 空白角落（离所有东西都远）→ 什么都不接（系统会放过去）
    assert R.hit_test(layout, 1, 1, hover_buttons=False, menu_open=False) is None


def test_chrome_hit_covers_sprite_nameplate_and_buttons():
    """`chrome_hit`：悬浮要显示/保持的那几块（统一两处判定，2026-09-27 修"闪"）。"""
    layout = R.compute_layout(
        scale=1.0, name="阿月", mood="高兴", text="还行", mute=False, menu_open=False
    )
    portrait = layout.portrait_hit or layout.portrait
    assert R.chrome_hit(layout, *center(portrait)) is True
    assert R.chrome_hit(layout, *center(layout.name_tag)) is True
    assert R.chrome_hit(layout, *center(layout.buttons[0].rect)) is True
    # 气泡那条带（立绘上方）不算"悬浮要显示"
    assert R.chrome_hit(layout, *center(layout.bubble)) is False


def test_chrome_hit_is_sticky_around_the_button_column():
    """2026-09-27（用户第二次反馈）：按钮要"够到就算、落在缝里也不消失"。

    用一张**窄**立绘把 `portrait_hit` 收窄，这样"按钮那一列"就落在立绘实体框**外面** ——
    正是用户鼠标移到按钮上的情形。
    """
    layout = R.compute_layout(
        scale=1.0, name="阿月", mood="高兴", text="还行", mute=False, menu_open=False
    )
    portrait = Image.new("RGBA", (60, 220), (226, 52, 47, 255))
    R.compose_frame(
        layout=layout,
        portrait=portrait,
        show_buttons=True,
        show_bubble=False,
        menu_open=False,
        active_scale=1.0,
    )
    assert layout.portrait_hit is not None
    pad = max(4, int(6 * layout.scale))
    first, second = layout.buttons[0], layout.buttons[1]
    mid_x = (first.rect.left + first.rect.right) // 2
    # 按钮之间的"缝"里 → 也算（整列都算）
    gap_y = (first.rect.bottom + second.rect.top) // 2
    assert R.chrome_hit(layout, mid_x, gap_y, pad=pad) is True
    # 列边往外一点点（pad 内）→ 也算
    # 2026-10-03：按钮改靠左 → "往外"是**左边**（往里才是立绘）
    mid_y = (first.rect.top + first.rect.bottom) // 2
    assert R.chrome_hit(layout, first.rect.left - pad + 1, mid_y, pad=pad) is True
    # 离列太远、又不在立绘实体框里 → 不算
    assert R.chrome_hit(layout, first.rect.left - pad - 20, mid_y, pad=pad) is False


def test_toolbar_reach_is_wider_than_the_buttons():
    """按钮面板在立绘实体**外面**，从立绘走过去要跨透明区。

    整块面板都要算"够到"，别在面板边缘就判出去（判出去 = 按钮消失，
    而透明区收不到鼠标消息 → 再也回不来）。
    """
    layout = R.compute_layout(
        scale=1.0, name="阿月", mood="高兴", text="还行", mute=False, menu_open=False
    )
    narrow = Image.new("RGBA", (60, 220), (226, 52, 47, 255))
    R.compose_frame(
        layout=layout,
        portrait=narrow,
        show_buttons=True,
        show_bubble=False,
        menu_open=False,
        active_scale=1.0,
    )
    toolbar = layout.toolbar
    assert toolbar is not None
    first = layout.buttons[0]
    pad = max(4, int(6 * layout.scale))
    mid_y = (first.rect.top + first.rect.bottom) // 2
    # 面板里面、按钮之间的缝 → 算够得到
    assert R.chrome_hit(layout, first.rect.right + 5, mid_y, pad=pad) is True
    # 面板外一点点（还在 pad 内）→ 仍算
    assert R.chrome_hit(layout, toolbar.right + pad - 1, mid_y, pad=pad) is True
    # 离面板太远 → 不算
    assert R.chrome_hit(layout, toolbar.right + pad + 20, mid_y, pad=pad) is False


def test_hit_test_buttons_only_when_hovering():
    layout = R.compute_layout(
        scale=1.0, name="阿月", mood="高兴", text="还行", mute=False, menu_open=False
    )
    button = layout.buttons[0]
    point = center(button.rect)

    assert R.hit_test(layout, *point, hover_buttons=False, menu_open=False) == "advance"
    assert R.hit_test(layout, *point, hover_buttons=True, menu_open=False) == f"button:{button.key}"


def test_hit_test_menu_rows():
    layout = R.compute_layout(
        scale=1.0, name="阿月", mood="高兴", text="还行", mute=False, menu_open=True
    )
    keys = [key for key, _ in layout.menu_rows]
    assert keys == ["scale:0.8", "scale:1.0", "scale:1.25", "scale:1.5", "exit"]

    key, rect = layout.menu_rows[0]
    assert R.hit_test(layout, *center(rect), hover_buttons=False, menu_open=True) == f"menu:{key}"


def test_compose_frame_is_transparent_where_nothing_is_drawn():
    layout = R.compute_layout(
        scale=1.0, name="阿月", mood="高兴", text="还行呀", mute=False, menu_open=False
    )
    frame = R.compose_frame(
        layout=layout,
        portrait=None,
        show_buttons=False,
        show_bubble=True,
        menu_open=False,
        active_scale=1.0,
    )

    assert frame.mode == "RGBA"
    assert frame.getpixel((1, 1))[3] == 0  # 左上角空白：完全透明
    assert frame.getpixel((layout.width - 2, layout.height - 2))[3] == 0
    bubble_center = ((layout.bubble.left + layout.bubble.right) // 2, (layout.bubble.top + layout.bubble.bottom) // 2)
    assert frame.getpixel(bubble_center)[3] > 0  # 气泡：不透明


def test_compose_frame_draws_the_portrait():
    layout = R.compute_layout(
        scale=1.0, name="阿月", mood="高兴", text="还行", mute=False, menu_open=False
    )
    portrait = Image.new("RGBA", (200, 400), (226, 52, 47, 255))

    frame = R.compose_frame(
        layout=layout,
        portrait=portrait,
        show_buttons=False,
        show_bubble=False,
        menu_open=False,
        active_scale=1.0,
    )

    x = layout.portrait.left + layout.portrait.width // 2
    y = layout.portrait.bottom - 10
    assert frame.getpixel((x, y))[:3] == (226, 52, 47)


def test_buttons_and_menu_are_drawn_on_demand():
    layout = R.compute_layout(
        scale=1.0, name="阿月", mood="高兴", text="还行", mute=False, menu_open=True
    )
    without = R.compose_frame(
        layout=layout,
        portrait=None,
        show_buttons=False,
        show_bubble=True,
        menu_open=False,
        active_scale=1.0,
    )
    with_ui = R.compose_frame(
        layout=layout,
        portrait=None,
        show_buttons=True,
        show_bubble=True,
        menu_open=True,
        active_scale=1.0,
    )

    point = center(layout.buttons[0].rect)
    assert without.getpixel(point)[3] == 0
    assert with_ui.getpixel(point)[3] > 0
    menu_point = center(layout.menu_rows[0][1])
    assert without.getpixel(menu_point)[3] == 0
    assert with_ui.getpixel(menu_point)[3] > 0


def test_each_icon_button_draws_something_distinct():
    """2026-09-27：按钮改成**矢量图标**（不写字）→ 每个都得画出点东西，而且各不相同。"""
    layout = R.compute_layout(
        scale=1.0, name="阿月", mood="高兴", text="还行", mute=False, menu_open=False
    )
    frame = R.compose_frame(
        layout=layout,
        portrait=None,
        show_buttons=True,
        show_bubble=False,
        menu_open=False,
        active_scale=1.0,
    )
    drawn = {}
    for button in layout.buttons:
        rect = button.rect
        box = frame.crop((rect.left, rect.top, rect.right, rect.bottom))
        assert box.getchannel("A").getbbox() is not None, button.key  # 有画东西
        drawn[button.key] = box.tobytes()
    assert len(set(drawn.values())) == len(drawn)


def test_mute_icon_follows_the_muted_state():
    def icon_pixels(muted: bool) -> bytes:
        layout = R.compute_layout(
            scale=1.0, name="阿月", mood="高兴", text="还行", mute=muted, menu_open=False
        )
        frame = R.compose_frame(
            layout=layout,
            portrait=None,
            show_buttons=True,
            show_bubble=False,
            menu_open=False,
            active_scale=1.0,
        )
        button = next(item for item in layout.buttons if item.key == "mute")
        rect = button.rect
        return frame.crop((rect.left, rect.top, rect.right, rect.bottom)).tobytes()

    assert icon_pixels(False) != icon_pixels(True)


def test_premultiplied_bgra_is_premultiplied():
    frame = Image.new("RGBA", (2, 1))
    frame.putpixel((0, 0), (255, 0, 0, 128))  # 半透明红
    frame.putpixel((1, 0), (0, 0, 0, 0))  # 全透明

    raw = premultiplied_bgra(frame)

    assert raw[0] == 0 and raw[1] == 0 and raw[2] == 128 and raw[3] == 128  # 预乘过的红
    assert raw[4:] == b"\x00\x00\x00\x00"


def center(rect: R.Rect) -> tuple[int, int]:
    return ((rect.left + rect.right) // 2, (rect.top + rect.bottom) // 2)

def test_bubble_is_only_drawn_when_asked():
    """D122：鼠标不在桌宠上 / 她没话说 → 那个框**根本不画**（整块透明）。"""
    layout = R.compute_layout(
        scale=1.0, name="阿月", mood="高兴", text="还行呀", mute=False, menu_open=False
    )
    bubble_center = (
        (layout.bubble.left + layout.bubble.right) // 2,
        (layout.bubble.top + layout.bubble.bottom) // 2,
    )
    with_bubble = R.compose_frame(
        layout=layout,
        portrait=None,
        show_buttons=False,
        show_bubble=True,
        menu_open=False,
        active_scale=1.0,
    )
    without = R.compose_frame(
        layout=layout,
        portrait=None,
        show_buttons=False,
        show_bubble=False,
        menu_open=False,
        active_scale=1.0,
    )

    assert with_bubble.getpixel(bubble_center)[3] > 0
    assert without.getpixel(bubble_center)[3] == 0
    # 2026-09-27：名字牌改成"悬浮才显示" -> 不悬浮时它是透明的
    assert without.getpixel(center(layout.name_tag))[3] == 0

def test_motion_transform_matches_the_keyframes():
    """D123：立绘动作照抄沉浸模式的 keyframes（位移按 layout 缩放）。"""
    # happy：0.28 那一刻应该跳到最高（-22px × scale）
    assert R.motion_transform("happy", 0.28, 1.0) == (0.0, -22.0, 1.02, 0.0)
    # 放大到 1.5 倍时位移跟着放大
    assert R.motion_transform("happy", 0.28, 1.5)[1] == -33.0
    # 中间点线性插值（0.28 → 0.56 之间）
    mid = R.motion_transform("happy", 0.42, 1.0)
    assert -22.0 < mid[1] < 0.0
    # 首尾都回到原位
    assert R.motion_transform("happy", 0.0, 1.0) == (0.0, 0.0, 1.0, 0.0)
    assert R.motion_transform("happy", 1.0, 1.0) == (0.0, 0.0, 1.0, 0.0)
    # 没见过的动作 = 不动
    assert R.motion_transform("nope", 0.5, 1.0) == (0.0, 0.0, 1.0, 0.0)


def test_motion_for_mood_maps_like_the_frontend():
    assert R.motion_for_mood("高兴") == "happy"
    assert R.motion_for_mood("有点害羞") == "shy"
    assert R.motion_for_mood("生气") == "angry"
    assert R.motion_for_mood("疑惑") == "puzzled"
    assert R.motion_for_mood("平静") == "plain"


# ── 动作变换缓存（2026-09-27 / D213，用户："换动作表情特别卡"）────────
def test_quantize_motion_rounds_to_cache_buckets():
    # 尺寸 0.5%、角度 0.5°：动作帧落进同一个桶才能命中缓存
    assert R.quantize_motion(1.0, 0.0) == (1.0, 0.0)
    assert R.quantize_motion(1.001, 0.2) == (1.0, 0.0)  # 1.0 附近视为"不用变换"
    assert R.quantize_motion(1.02, -1.26) == (1.02, -1.5)
    assert R.quantize_motion(0.996, 3.0) == (0.995, 3.0)


def test_transform_motion_is_identity_when_nothing_to_do():
    image = Image.new("RGBA", (20, 30), (1, 2, 3, 255))
    assert R.transform_motion(image, 1.0, 0.0) is image
    assert R.transform_motion(image, 1.002, 0.2) is image  # 落回 1.0/0.0


def test_transform_motion_caches_and_keeps_size_on_rotate():
    """旋转用 expand=False → **尺寸不变**（expand=True 会被下面兜底再缩一次，每帧白花 10ms）。"""
    image = Image.new("RGBA", (40, 60), (10, 20, 30, 255))
    cache = R.TransformCache()
    first = R.transform_motion(image, 1.0, 3.0, cache)
    assert first is not image
    assert first.size == image.size  # expand=False
    second = R.transform_motion(image, 1.0, 3.0, cache)
    assert second is first  # 命中缓存


def test_transform_cache_never_serves_another_image():
    """键里带 `id(image)`：源图被回收、id 复用也不能串图。"""
    cache = R.TransformCache()
    one = Image.new("RGBA", (8, 8), (255, 0, 0, 255))
    other = Image.new("RGBA", (8, 8), (0, 255, 0, 255))
    key = (id(one), 1.0, 3.0)
    cache.put(key, one, one.copy())
    assert cache.get(key, one) is not None
    assert cache.get(key, other) is None  # 同一把键、不同源图 → 不算命中


def test_transform_cache_respects_its_byte_budget():
    cache = R.TransformCache(budget_bytes=8 * 8 * 4 * 2 + 1)
    keys = []
    for index in range(4):
        source = Image.new("RGBA", (8, 8), (index, 0, 0, 255))
        key = (id(source), 1.0, float(index))
        cache.put(key, source, Image.new("RGBA", (8, 8), (index, 0, 0, 255)))
        keys.append((key, source))
    # 预算只够两张 → 最早的被淘汰，最近的还在
    assert cache.get(keys[0][0], keys[0][1]) is None
    assert cache.get(keys[-1][0], keys[-1][1]) is not None
