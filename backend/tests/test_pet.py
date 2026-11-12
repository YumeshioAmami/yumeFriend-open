"""桌宠"窗口这件事"里能离线测的那部分（T2.2-4 / D119~D121）。

真正的窗口开不起来（也不该在单测里开），所以这里盯的是
**纯函数 + 状态文件 + 气泡定位的数学**：

· `clamp_scale` / `scaled_size` —— 档位、尺寸
· `load_state` / `save_state` —— 位置/缩放记在本机，坏了也不能炸
· `compute_anchor_target` —— 气泡弹在桌宠正上方 / 贴边夹回来 / 上方不够翻下面
· `offscreen_delta` —— 放大之后别怼出屏幕
· `PetWindow` 的壳（不碰真窗口也能验证状态）
"""

from __future__ import annotations

import json

import pytest

from backend.desktop import popup
from backend.desktop import pet_render as R
from backend.desktop import pet_native
from backend.desktop.pet import DEFAULT_SCALE, PetWindow, clamp_scale, load_state, save_state, scaled_size
from backend.desktop.pet_native import (
    POLL_MS,
    STREAM_POLL_MS,
    PetNativeWindow,
    offscreen_delta,
    resized_scale,
)


# ── 语音"谁出声"（v3.14 / D182） ─────────────────────────────────────
def _quiet_pet(tmp_path, monkeypatch, *, in_front: bool) -> PetNativeWindow:
    """造一个不会真开窗、不会真起播放线程的桌宠。"""
    pet = PetNativeWindow("http://127.0.0.1:9", state_path=tmp_path / "pet.json")
    pet._visible = True
    pet._ensure_voice_thread = lambda: None  # 别真去连 HTTP
    monkeypatch.setattr(pet, "_main_window_in_front", lambda: in_front)
    return pet


def test_pet_stays_quiet_when_the_main_window_is_in_front(tmp_path, monkeypatch):
    """主窗口在前台 → 这一条归**页面**放，桌宠别插嘴（不然两段声音叠一起，用户实测过）。"""
    pet = _quiet_pet(tmp_path, monkeypatch, in_front=True)
    message = {"id": "m1", "audio_segments": [{"url": "/a/0"}, {"url": "/a/1"}]}
    pet._maybe_play(message)
    assert pet._voice_queue.empty(), "页面在放，桌宠不该排队"
    assert "m1" in pet._page_owned

    # 窗口后来切走了也**不能补播**（不然就跟她说重了）
    pet._main_window_in_front = lambda: False  # type: ignore[method-assign]
    pet._maybe_play(message)
    assert pet._voice_queue.empty()


def test_pet_speaks_when_nobody_is_looking(tmp_path, monkeypatch):
    """主窗口不在前台（你在别的程序里 / 沉浸没开）→ 桌宠出声。"""
    pet = _quiet_pet(tmp_path, monkeypatch, in_front=False)
    pet._maybe_play({"id": "m1", "audio_url": "/a/whole"})
    assert pet._voice_queue.qsize() == 1
    assert "m1" not in pet._page_owned


# ── 纯函数 ──────────────────────────────────────────────────────────
def test_clamp_scale_is_free_but_bounded():
    """D122：自由缩放 —— 中间的值都收，只挡"缩没"和"撑爆"两头。"""
    assert clamp_scale(1.0) == 1.0
    assert clamp_scale(1.37) == 1.37
    assert clamp_scale(9) == 9.0   # 2026-09-27：上限放开（原来 3.0）
    assert clamp_scale(0.1) == 0.4


def test_clamp_scale_survives_garbage():
    """JS / 状态文件那边什么都可能传过来 —— 不许抛错。"""
    assert clamp_scale(None) == DEFAULT_SCALE
    assert clamp_scale("不是数字") == DEFAULT_SCALE
    assert clamp_scale(object()) == DEFAULT_SCALE
    assert clamp_scale("1.25") == 1.25


def test_scaled_size_follows_the_step():
    base = scaled_size(1.0)
    assert scaled_size(0.8) == (int(base[0] * 0.8), int(base[1] * 0.8))
    assert scaled_size(1.5) == (int(base[0] * 1.5), int(base[1] * 1.5))
    # 2026-11-07 用户："不要设置大小限制，现在的太小了" → 窗口跟着角色倍率走。
    huge = scaled_size(10.0)
    assert huge[0] == int(380 * 10.0)
    assert huge[1] == int(560 * 10.0)


# ── 状态文件 ────────────────────────────────────────────────────────
def test_state_round_trip(tmp_path):
    path = tmp_path / "pet_window.json"
    save_state({"x": 10, "y": 20, "scale": 1.25, "friend_id": "f1"}, path)

    assert load_state(path) == {"x": 10, "y": 20, "scale": 1.25, "friend_id": "f1"}


def test_state_file_missing_or_broken_is_not_fatal(tmp_path):
    missing = tmp_path / "nope.json"
    assert load_state(missing) == {}

    broken = tmp_path / "broken.json"
    broken.write_text("{ 不是 json", encoding="utf-8")
    assert load_state(broken) == {}
    broken.write_text("[1,2,3]", encoding="utf-8")
    assert load_state(broken) == {}


def test_pet_window_remembers_what_it_read(tmp_path):
    path = tmp_path / "pet_window.json"
    save_state({"x": 300, "y": 400, "scale": 1.5, "friend_id": "friend-1"}, path)

    pet = PetWindow("http://127.0.0.1:9", state_path=path)

    assert pet.scale == 1.5
    assert pet.friend_id == "friend-1"
    assert pet.is_open is False
    assert pet.is_visible is False
    assert pet.rect_if_visible() is None


def test_pet_window_start_state_is_sane_without_a_file(tmp_path):
    pet = PetWindow("http://127.0.0.1:9", state_path=tmp_path / "none.json")

    assert pet.scale == DEFAULT_SCALE
    assert pet.friend_id is None


def test_set_scale_clamps_and_persists_without_a_window(tmp_path):
    path = tmp_path / "pet_window.json"
    pet = PetWindow("http://127.0.0.1:9", state_path=path)

    pet._window._scale = 1.0  # 不建窗口，直接验换算 + 存盘
    pet._window._save_state()
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["scale"] == 1.0


def test_locked_mode_blocks_scale_and_drag_and_remembers_itself(tmp_path):
    path = tmp_path / "pet_window.json"
    pet = PetNativeWindow("http://127.0.0.1:9", state_path=path)
    pet._scale = 1.5
    pet._locked = True
    pet._layout = R.compute_layout(
        scale=1.5,
        ui_scale=1.0,
        locked=True,
        name="阿月",
        mood="高兴",
        text="还行",
        mute=False,
        menu_open=False,
    )
    pet._rect = (0, 0, pet._layout.width, pet._layout.height)

    # 锁定后缩放请求不再生效
    assert pet._set_scale_on_thread(2.0) == 1.5
    # 点角色不产生拖动
    point = (
        (pet._layout.portrait.left + pet._layout.portrait.right) // 2,
        (pet._layout.portrait.top + pet._layout.portrait.bottom) // 2,
    )
    pet._on_press(point)
    assert pet._drag is None

    pet._save_state()
    assert json.loads(path.read_text(encoding="utf-8"))["locked"] is True
    assert PetNativeWindow("http://127.0.0.1:9", state_path=path)._locked is True


def test_shell_api_reports_state(tmp_path):
    pet = PetWindow("http://127.0.0.1:9", state_path=tmp_path / "pet_window.json")

    assert pet.api.pet_state() == {
        "open": False,
        "visible": False,
        "friend_id": None,
        "scale": DEFAULT_SCALE,
    }
    assert pet.api.hide_pet() == {"ok": True}


# ── 气泡定位（D119 第 6 条）──────────────────────────────────────────
@pytest.fixture()
def screen(monkeypatch):
    """假装一块 1920x1080、工作区不含任务栏的屏（右下角 y=1040）。"""

    def fake_work_area_at_point(x, y):  # noqa: ARG001
        return 0, 0, 1920, 1040

    monkeypatch.setattr(popup.win32, "work_area_at_point", fake_work_area_at_point)
    return 0, 0, 1920, 1040


def test_bubble_goes_above_the_pet_centered(screen):
    x, y = popup.compute_anchor_target(popup_size=(372, 104), anchor=(1400, 700, 1740, 1170))

    assert x == (1400 + 1740) // 2 - 372 // 2
    assert y == 700 - popup.GAP - 104


def test_bubble_is_clamped_into_the_screen(screen):
    x, _ = popup.compute_anchor_target(popup_size=(372, 104), anchor=(1800, 700, 1900, 1170))
    assert x + 372 <= 1920
    x, _ = popup.compute_anchor_target(popup_size=(372, 104), anchor=(0, 700, 120, 1170))
    assert x >= 0


def test_bubble_flips_below_when_there_is_no_room_above(screen):
    _, y = popup.compute_anchor_target(popup_size=(372, 104), anchor=(600, 2, 940, 470))

    assert y == 470 + popup.GAP


# ── 缩放之后别把自己怼出屏幕 ────────────────────────────────────────
def test_offscreen_delta_is_zero_when_inside():
    assert offscreen_delta((100, 100, 400, 500), (0, 0, 1920, 1040)) == (0, 0)


def test_offscreen_delta_pulls_back_the_overflowing_edges():
    assert offscreen_delta((2000, 700, 2644, 1700), (0, 0, 2560, 1528)) == (-84, -172)
    assert offscreen_delta((-50, -30, 300, 400), (0, 0, 1920, 1040)) == (50, 30)


def test_streaming_reply_polls_faster_and_queues_new_characters(tmp_path, monkeypatch):
    """D463：回复流式进行中时，桌宠要跟着正文增长，而不是整段停住。"""
    pet = PetNativeWindow("http://127.0.0.1:9", state_path=tmp_path / "pet.json")
    monkeypatch.setattr(pet, "_arm_speech", lambda *args, **kwargs: None)
    monkeypatch.setattr(pet, "_play_mood_sound", lambda *args, **kwargs: None)
    friends = [
        {
            "id": "f1",
            "name": "阿月",
            "live2d_model_id": None,
            "presence": True,
        }
    ]
    message = {
        "id": "m1",
        "role": "assistant",
        "content": "你好",
        "stream_status": "streaming",
        "moods": ["平静"],
    }

    pet._apply_data(friends, "f1", [message], initial=True)
    assert pet._poll_ms == STREAM_POLL_MS
    assert pet._paragraphs == ["你好"]

    # 同一条消息增长：新增字也要进逐字队列，而不是瞬间整段跳出来。
    grown = dict(message, content="你好呀，我在")
    pet._apply_data(friends, "f1", [grown], initial=False)
    assert pet._paragraphs == ["你好呀，我在"]
    assert pet._reveal_left == len("你好呀，我在")

    # 说完了 → 恢复普通轮询；正文内容没变，不再重复排队。
    finished = dict(grown, stream_status="completed")
    pet._apply_data(friends, "f1", [finished], initial=False)
    assert pet._poll_ms == POLL_MS


def test_visual_mood_relaxes_then_randomises_without_changing_real_mood(tmp_path, monkeypatch):
    """D465：说完几秒回正常；空闲随机表情只改脸，不改真实 mood / 历史。"""
    pet = PetNativeWindow("http://127.0.0.1:9", state_path=tmp_path / "pet.json")
    pet._live2d = type("DummyLive2D", (), {"active": True})()
    pet._moods = ["高兴"]
    pet._index = 0
    pet._speech_until = 0.0
    pet._visual_was_speaking = True
    pet._visual_relax_until = 1005.0
    now = [1000.0]
    monkeypatch.setattr(pet_native.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(
        pet_native.live2d_settings_service,
        "labels_for_friend",
        lambda *args, **kwargs: ["正常", "高兴", "疑惑", "生气"],
    )
    monkeypatch.setattr(pet_native.random, "choice", lambda seq: seq[0])
    monkeypatch.setattr(pet_native.random, "uniform", lambda low, high: high)

    # 刚说完：保持真实情绪
    assert pet._visual_mood() == "高兴"
    # 5 秒后：回正常
    now[0] = 1005.1
    assert pet._visual_mood() == "正常"
    # 到达下一次空闲表情时间：随机挑一个非正常、且不是“真的生气”等重情绪
    now[0] = pet._next_idle_visual_at
    picked = pet._visual_mood()
    assert picked == "疑惑"  # 也不会重复当前真实表情“高兴”
    assert pet._moods == ["高兴"], "随机表情不能污染真实消息情绪"
    assert pet._mood() == "高兴"


def test_visual_mood_does_not_randomise_while_user_is_interacting(tmp_path, monkeypatch):
    pet = PetNativeWindow("http://127.0.0.1:9", state_path=tmp_path / "pet.json")
    pet._live2d = type("DummyLive2D", (), {"active": True})()
    pet._hovered = True
    pet._next_idle_visual_at = 1.0
    monkeypatch.setattr(pet_native.time, "monotonic", lambda: 100.0)
    assert pet._visual_mood() == "正常"
    assert pet._idle_visual_mood == ""
def test_resized_scale_grows_and_shrinks_proportionally():
    """D122 自由缩放：按住抓角拖时按"离左上角的距离"比例变，两头夹住。"""
    assert resized_scale(1.0, 800, 400) == 2.0  # 拖远一倍 → 放大一倍
    assert resized_scale(1.0, 200, 400) == 0.5  # 拉近一半 → 缩小一半
    assert resized_scale(1.0, 100, 400) == 0.4  # 不会缩没
    assert resized_scale(1.5, 4000, 400) == 10.0  # 2026-09-27：上限放开（原来是 3.0）
    assert resized_scale(1.0, 400.0, 0.0) == 10.0  # dist0 为 0 时不会炸（夹到上限）

def test_pet_native_has_no_undefined_constants():
    """防呆：`pet_native.py` 里用到的大写常量必须真的定义过（静态查一遍）。

    （踩过：`SWP_NOZORDER` 没定义 → 每次挪窗口都在窗口线程里 `NameError` →
    表现是"**拖不动**"、输入框也从来没被摆出来；因为是窗口线程里 catch 掉的，
    光看代码/单测都发现不了。这个检查就是为它写的。）
    """
    import ast
    import re
    from pathlib import Path

    from backend.desktop import pet_native as native

    source = Path(native.__file__).read_text(encoding="utf-8")
    used = {
        node.id
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.Name) and re.fullmatch(r"[A-Z][A-Z0-9_]+", node.id)
    }
    missing = sorted(name for name in used if not hasattr(native, name))

    assert missing == [], f"这些常量用了但没定义：{missing}"
def test_logo_artifacts_all_exist():
    """D132：四处图标（界面 / 托盘 / favicon / 打包+快捷方式）都得在，而且是同一张图生成的。

    用户点名要"托盘、快捷方式、任务栏、主界面左上角、窗口左上角**都**换成这个"——
    这条测试就是盯着"别漏了某一处"。
    """
    from backend.config import ROOT_DIR

    artifacts = [
        ROOT_DIR / "assets" / "yumeFriend-large.png",  # 成品（512，透明圆角）
        ROOT_DIR / "assets" / "yumeFriend.png",  # 打包 / 托盘用
        ROOT_DIR / "assets" / "yumeFriend.ico",  # 桌面快捷方式 + 窗口/任务栏
        ROOT_DIR / "frontend" / "public" / "logo-192.png",  # 主界面左上角
        ROOT_DIR / "frontend" / "public" / "favicon.png",  # 浏览器页签
    ]
    for path in artifacts:
        assert path.is_file(), f"{path} 不在"
        assert path.stat().st_size > 1000, f"{path} 太小，像是空的"


def test_app_icon_reads_the_new_logo():
    """托盘/窗口图标读的是那张成品图（不是旧的 ゆ 手绘）。"""
    from backend.desktop import icon as icon_module

    image = icon_module.app_icon(64)
    assert image.size == (64, 64)
    # 成品图四角是透明圆角（旧的手绘版四角也是透明，但中心是橙色 —— 这张中心也是暖色）
    assert image.getpixel((1, 1))[3] < 40
    center = image.getpixel((32, 32))
    assert center[0] > center[2], "中心应该是暖色（橙/棕），不是冷色"


def test_fitted_portrait_hits_the_cache_without_reloading(tmp_path):
    """2026-09-27（用户："换动作表情会特别卡顿特别慢"）：

    换情绪**不该再解一遍大图**（实测一次解 1536×2048 ≈ 51ms + resize ≈ 16ms）。
    缓存命中时连 `_load_portrait` 都不该调。
    """
    from PIL import Image

    from backend.desktop import pet_render as R

    pet = PetNativeWindow("http://127.0.0.1:9", state_path=tmp_path / "pet.json")
    calls = {"n": 0}

    def fake_load():
        calls["n"] += 1
        return Image.new("RGBA", (40, 60), (10, 20, 30, 255))

    pet._load_portrait = fake_load  # type: ignore[method-assign]
    pet._friend_id = "fid"
    pet._index = 0
    pet._moods = ["高兴"]
    area = R.Rect(0, 0, 120, 180)

    first = pet._fitted_portrait(area)
    second = pet._fitted_portrait(area)
    assert first is not None and second is first  # 第二次直接拿缓存
    assert calls["n"] == 1  # 只解了一次

    pet._moods = ["生气"]
    pet._fitted_portrait(area)
    assert calls["n"] == 2  # 新情绪要解一张

    pet._moods = ["高兴"]
    assert pet._fitted_portrait(area) is first  # 换回来还是缓存
    assert calls["n"] == 2


# ── 换朋友按钮（2026-09-27 / D213，用户："换朋友按钮没用"）─────────────
def _friends(*, present: int) -> list[dict]:
    return [
        {"id": f"f{index}", "name": f"朋友{index}", "presence": index < present}
        for index in range(5)
    ]


def test_switch_friend_walks_everyone_when_only_one_is_present(tmp_path):
    """只有一个在线时也要能换 —— 以前池子只留"在线的"，凑不够两个就直接 return。"""
    pet = PetNativeWindow("http://127.0.0.1:9", state_path=tmp_path / "pet.json")
    pet._friends = _friends(present=1)
    pet._friend_id = "f3"
    pet._switch_friend()
    assert pet._friend_id == "f4"  # 全部朋友里轮换，不再原地不动


def test_switch_friend_prefers_present_friends_when_there_are_two(tmp_path):
    pet = PetNativeWindow("http://127.0.0.1:9", state_path=tmp_path / "pet.json")
    pet._friends = _friends(present=2)  # f0 / f1 在线
    pet._friend_id = "f0"
    pet._switch_friend()
    assert pet._friend_id == "f1"
    pet._switch_friend()
    assert pet._friend_id == "f0"  # 只在在线的两个人里转


def test_switch_friend_does_nothing_when_alone(tmp_path):
    pet = PetNativeWindow("http://127.0.0.1:9", state_path=tmp_path / "pet.json")
    pet._friends = _friends(present=1)[:1]
    pet._friend_id = "f0"
    pet._switch_friend()
    assert pet._friend_id == "f0"


# ── 输入框字号跟着缩放（2026-09-27 / D213，用户："输入框太小、光标像没有"）──
def test_edit_font_height_follows_the_scale():
    assert PetNativeWindow._edit_font_height(1.0) >= 14
    assert PetNativeWindow._edit_font_height(2.46) > PetNativeWindow._edit_font_height(1.0) * 2
    assert PetNativeWindow._edit_font_height(0.1) == 14  # 再缩也不小于 14，别缩成看不见


# ── 桌宠「回到主界面」：只在最小化时还原（2026-09-27 / D213）──────────
class _FakeUser32:
    def __init__(self, *, iconic: bool) -> None:
        self.iconic = iconic
        self.calls: list[tuple] = []

    def IsIconic(self, _hwnd: int) -> int:  # noqa: N802 —— Win32 名字
        return 1 if self.iconic else 0

    def ShowWindow(self, _hwnd: int, cmd: int) -> int:  # noqa: N802
        self.calls.append(("show", cmd))
        return 1

    def SetForegroundWindow(self, _hwnd: int) -> int:  # noqa: N802
        self.calls.append(("front",))
        return 1


def _fake_bring_to_front(monkeypatch, *, iconic: bool) -> _FakeUser32:
    from backend.desktop import win32

    fake = _FakeUser32(iconic=iconic)
    monkeypatch.setattr(win32, "user32", lambda: fake)
    win32.bring_to_front(1234)
    return fake


def test_bring_to_front_does_not_unmaximize(monkeypatch):
    """无脑 `restore()` 会把最大化的主窗口打回普通大小 —— 只有最小化才许还原。"""
    from backend.desktop import win32

    fake = _fake_bring_to_front(monkeypatch, iconic=False)
    assert fake.calls == [("show", win32.SW_SHOW), ("front",)]


def test_bring_to_front_restores_a_minimized_window(monkeypatch):
    from backend.desktop import win32

    fake = _fake_bring_to_front(monkeypatch, iconic=True)
    assert fake.calls == [("show", win32.SW_RESTORE), ("front",)]
