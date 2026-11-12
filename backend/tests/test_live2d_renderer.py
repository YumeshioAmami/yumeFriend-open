"""桌宠 Live2D 的纯逻辑测试（不要求显卡 / 不加载真模型）。"""

from __future__ import annotations

import ast
import json
from pathlib import Path

from PIL import Image, ImageDraw

from backend import config
from backend.desktop.live2d_renderer import (
    Live2DRenderer,
    expression_candidates_for_mood,
    expression_for_mood,
    find_model_file,
    mood_expression_kind,
    prepare_runtime_model,
    resolve_model_file,
    watermark_off_params,
)
from backend.services import live2d_model_registry


def test_live2d_animation_clock_is_high_resolution():
    """Windows 的 time.monotonic() 只有 ~15.6ms 分辨率，120fps 下 delta 会 0/16ms 交替。

    Live2D 的动画/物理时间轴必须统一用 perf_counter，否则角色会两帧一抽。
    """
    from backend.desktop import live2d_renderer as module

    tree = ast.parse(Path(module.__file__).read_text(encoding="utf-8"))
    monotonic_calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "time"
        and node.func.attr == "monotonic"
    ]
    assert monotonic_calls == []


def test_mood_expression_mapping_covers_core_emotions():
    assert expression_for_mood("高兴") == "happy"
    assert expression_for_mood("害羞") == "shy"
    assert expression_for_mood("心动") == "love"
    assert expression_for_mood("难过") == "sad"
    assert expression_for_mood("紧张") == "nervous"
    assert expression_for_mood("得意") == "smug"
    assert expression_for_mood("平静") is None


def test_expression_candidates_cover_real_free_model_names():
    """C 中肥鱼的真实 exp3 是 blush / eyes_oo / mouth_worry 这类名字。"""
    shy = expression_candidates_for_mood("害羞")
    assert "shy" in shy and "blush" in shy
    surprised = expression_candidates_for_mood("惊讶")
    assert "surprised" in surprised and "eyes_oo" in surprised
    assert "happy" in expression_candidates_for_mood("高兴")


def test_mood_expression_kind_separates_excited_from_calm():
    assert mood_expression_kind("高兴") == "happy"
    assert mood_expression_kind("兴奋") == "excited"
    assert mood_expression_kind("害羞") == "shy"
    assert mood_expression_kind("生气") == "angry"
    assert mood_expression_kind("平静") == "calm"


def test_all_default_portrait_tags_have_distinct_expression_kinds():
    """默认立绘的 20 个差分不能被吞成同一个 calm。"""
    expected = {
        "担心": "worried",
        "调皮": "smug",
        "高兴": "happy",
        "害怕": "afraid",
        "害羞": "shy",
        "慌张": "afraid",
        "紧张": "nervous",
        "惊讶": "surprised",
        "平静": "calm",
        "认真": "serious",
        "伤心": "sad",
        "生气": "angry",
        "无奈": "helpless",
        "心动": "love",
        "兴奋": "excited",
        "羞耻": "shy",
        "厌恶": "angry",
        "疑惑": "confused",
        "正常": "calm",
        "自信": "smug",
    }
    actual = {tag: mood_expression_kind(tag) for tag in expected}
    assert actual == expected


def test_find_model_file_prefers_default_and_skips_runtime(tmp_path):
    default = tmp_path / "default"
    default.mkdir()
    (default / "other.model3.json").write_text("{}", encoding="utf-8")
    (default / "_yumefriend.model3.json").write_text("{}", encoding="utf-8")
    found = find_model_file(tmp_path)
    assert found is not None
    assert found.name == "other.model3.json"


def test_prepare_runtime_model_adds_expressions_and_idle_motion(tmp_path):
    model_dir = tmp_path / "model"
    model_dir.mkdir()
    (model_dir / "model.model3.json").write_text(
        json.dumps(
            {
                "Version": 3,
                "FileReferences": {"Moc": "model.moc3", "Textures": []},
            }
        ),
        encoding="utf-8",
    )
    (model_dir / "Exp3").mkdir()
    (model_dir / "Exp3" / "脸红.exp3.json").write_text("{}", encoding="utf-8")
    (model_dir / "Animation").mkdir()
    (model_dir / "Animation" / "IDLE.motion3.json").write_text("{}", encoding="utf-8")

    runtime = prepare_runtime_model(model_dir / "model.model3.json")
    data = json.loads(runtime.read_text(encoding="utf-8"))
    refs = data["FileReferences"]
    assert refs["Expressions"][0]["Name"] == "shy"
    assert refs["Expressions"][0]["File"] == "Exp3/脸红.exp3.json"
    assert refs["Motions"]["Idle"][0]["File"] == "Animation/IDLE.motion3.json"
    assert any(group["Name"] == "LipSync" for group in data["Groups"])


def test_resolve_model_file_prefers_friend_override(tmp_path, monkeypatch):
    friend_dir = tmp_path / "friends" / "fid"
    friend_dir.mkdir(parents=True)
    (friend_dir / "friend.model3.json").write_text("{}", encoding="utf-8")
    default_dir = tmp_path / "default"
    default_dir.mkdir()
    (default_dir / "arch.model3.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(config, "LIVE2D_FRIEND_DIR", tmp_path / "friends")
    monkeypatch.setattr(config, "DEFAULT_LIVE2D_DIR", default_dir)
    monkeypatch.setattr(config, "LIVE2D_DIR", tmp_path / "legacy")

    assert resolve_model_file("fid").name == "friend.model3.json"
    assert resolve_model_file("other").name == "arch.model3.json"


def test_resolve_model_file_prefers_friend_selected_model_id(tmp_path, monkeypatch):
    shared_dir = tmp_path / "shared" / "m1"
    shared_dir.mkdir(parents=True)
    (shared_dir / "m.moc3").write_bytes(b"moc3")
    shared_file = shared_dir / "m.model3.json"
    shared_file.write_text(
        json.dumps(
            {
                "Version": 3,
                "FileReferences": {"Moc": "m.moc3", "Textures": []},
            }
        ),
        encoding="utf-8",
    )
    default_dir = tmp_path / "default"
    default_dir.mkdir()
    (default_dir / "arch.model3.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(config, "DEFAULT_LIVE2D_DIR", default_dir)
    monkeypatch.setattr(config, "LIVE2D_DIR", tmp_path / "shared")
    monkeypatch.setattr(config, "LIVE2D_FRIEND_DIR", tmp_path / "friends")

    info = live2d_model_registry.model_info_for_file(shared_file)
    assert info is not None
    assert resolve_model_file("fid", info.id) == shared_file.resolve()


def test_watermark_off_params_reads_builtin_expression(tmp_path):
    exp = tmp_path / "expressions"
    exp.mkdir()
    (exp / "no_watermark.exp3.json").write_text(
        json.dumps(
            {
                "Type": "Live2D Expression",
                "Parameters": [{"Id": "Paramcol_3", "Value": 1.0, "Blend": "Add"}],
            }
        ),
        encoding="utf-8",
    )
    assert watermark_off_params(tmp_path) == {"Paramcol_3": 1.0}


def test_stable_crop_box_freezes_after_first_measurement():
    """第一帧量完就冻结：呼吸/摆头让内容变大时也不能再改裁剪框。

    以前是"只扩不缩"：外接框慢慢变大 → 输出图尺寸跟着变 → 桌宠 GDI 重新缩放，
    看起来就是持续抖动。
    """
    renderer = Live2DRenderer()
    frame = Image.new("RGBa", (200, 300), (0, 0, 0, 0))
    draw = ImageDraw.Draw(frame)
    draw.rectangle((80, 60, 120, 200), fill=(255, 255, 255, 255))
    first = renderer._stable_crop_box(frame)

    wider = Image.new("RGBa", (200, 300), (0, 0, 0, 0))
    draw = ImageDraw.Draw(wider)
    draw.rectangle((60, 58, 145, 202), fill=(255, 255, 255, 255))
    second = renderer._stable_crop_box(wider)

    assert second == first


def test_idle_offsets_include_body_sway_and_vary_breath():
    """D465：待机不是固定正弦；要有可见的身体左右/前后晃动和随机呼吸变奏。"""
    renderer = Live2DRenderer()
    renderer._settings = {
        "idle": {
            "enabled": True,
            "head_x": 2.0,
            "head_y": 1.2,
            "head_z": 1.5,
            "body_x": 1.2,
            "body_z": 0.8,
            "gaze_x": 0.14,
            "gaze_y": 0.10,
            "breath": 0.24,
        }
    }
    renderer._idle_next_at = 0.0
    renderer._breath_next_at = 0.0
    phase = renderer._breath_phase
    renderer._update_idle_offsets(10.0, 0.1)

    assert "body_y" in renderer._idle_target
    assert set(renderer._idle_target) == {
        "head_x",
        "head_y",
        "head_z",
        "body_x",
        "body_y",
        "body_z",
        "gaze_x",
        "gaze_y",
    }
    assert renderer._breath_phase > phase
    assert 0.85 <= renderer._breath_rate <= 1.65
    assert 0.72 <= renderer._breath_gain <= 1.28
    assert 2.4 <= renderer._idle_next_at - 10.0 <= 6.8
