import json
from pathlib import Path


PRESETS_PATH = Path(__file__).resolve().parents[2] / "frontend" / "public" / "live2d" / "mood_presets.json"


def _data():
    return json.loads(PRESETS_PATH.read_text(encoding="utf-8"))


def test_all_portrait_emotions_have_a_preset():
    data = _data()
    expected = {
        "高兴",
        "兴奋",
        "平静",
        "正常",
        "害羞",
        "羞耻",
        "心动",
        "担心",
        "伤心",
        "无奈",
        "生气",
        "厌恶",
        "惊讶",
        "害怕",
        "慌张",
        "紧张",
        "疑惑",
        "认真",
        "自信",
        "调皮",
    }
    assert set(data["presets"]) == expected
    assert "PARAM_EXP4" in data["base"]
    assert data["base"]["PARAM_EXP4"] == 0


def test_low_and_mid_emotions_are_not_positive_placeholders():
    presets = _data()["presets"]
    assert presets["伤心"]["params"]["ParamEyeLOpen"] <= 0.2
    assert presets["伤心"]["params"]["ParamEyeSmile"] == -1
    assert presets["伤心"]["params"]["ParamMouthForm"] < -0.8
    assert presets["无奈"]["params"]["ParamMouthX"] > 0
    assert presets["担心"]["params"]["PARAM_EXP7"] == -1
    assert presets["紧张"]["params"]["PARAM_EXP8"] == -1
    assert presets["生气"]["params"]["PARAM_EXP3"] == 1
    assert presets["厌恶"]["params"]["PARAM_EXP8"] == -1
    assert presets["认真"]["params"]["ParamMouthForm"] < 0


def test_happy_and_shy_have_their_own_component_mix():
    presets = _data()["presets"]
    assert "PARAM_EXP2" not in presets["高兴"]["params"]
    assert presets["高兴"]["params"]["PARAM_EXP4"] == 1
    assert presets["高兴"]["params"]["ParamEyeLOpen"] == 1
    assert presets["高兴"]["params"]["ParamEyeSmile"] == 0
    assert presets["兴奋"]["params"]["PARAM_EXP8"] == 1
    assert presets["害羞"]["params"]["PARAM_EXP4"] == 1
    assert presets["羞耻"]["params"]["PARAM_EXP3"] == 1
    assert presets["心动"]["params"]["PARAM_EXP7"] == 1
