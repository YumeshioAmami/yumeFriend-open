"""v2 设置：**按用途**的模型配置（D80 / T2.0-2）。

这个测试盯三件最容易出错的事：

1. `api_key` 明文**永远不出后端**（跟 v1 同一条铁律，api.md §5.1）
2. 合并语义：不传 = 沿用 / 空串 = 清空 / 非空 = 覆盖
3. **老调用点一行都不用改**：`llm_config()` 优先读 `purposes.chat`，读不到退回老字段

"""

from __future__ import annotations

import json

import pytest

import backend.config as config
import backend.db as db
from backend.llm.errors import LLMConfigError
from backend.llm.factory import resolve_purpose_config
from backend.services import settings_service


@pytest.fixture()
def session(tmp_path, monkeypatch):
    db_file = tmp_path / "purposes.db"
    monkeypatch.setattr(config, "DB_PATH", db_file)
    monkeypatch.setattr(db, "DB_PATH", db_file)
    monkeypatch.setattr(db, "_engine", None)
    monkeypatch.setattr(db, "_session_factory", None)
    from backend.db import Base, get_engine

    Base.metadata.create_all(get_engine())
    return db.session_scope


def _chat(base_url="https://example.com/v1", model="m1", api_key="sk-secret") -> dict:
    return {"mode": "api", "base_url": base_url, "model": model, "api_key": api_key, "proxy": "none"}


def test_defaults_fill_all_five_purposes_and_vision_follows_chat(session):
    with session() as s:
        public = settings_service.public_purposes(s)

    assert set(public) == set(settings_service.PURPOSE_NAMES)
    assert public["vision"]["follow_chat"] is True  # D79：chat 模型本身就能看图
    assert public["chat"] == {}


def test_game_fast_purpose_can_point_to_local_ollama(session):
    """D279：游戏快脑可以单独配本地 Ollama，不占聊天模型。"""
    conf = {
        "mode": "local",
        "base_url": "http://127.0.0.1:11434/v1",
        "model": "qwen2.5:3b-instruct",
        "proxy": "none",
    }
    with session() as s:
        settings_service.merge_purposes(s, {"game_fast": conf})
    with session() as s:
        row = settings_service.get(s)
        assert settings_service.purpose_enabled_from(row, "game_fast") is True
        public = settings_service.public_purposes(s)
        assert public["game_fast"]["base_url"] == conf["base_url"]
        assert public["game_fast"]["model"] == conf["model"]
        resolved = resolve_purpose_config(row.purposes, "game_fast")
        assert resolved.base_url == conf["base_url"]
        assert resolved.model == conf["model"]


def test_public_purposes_never_leaks_api_key(session):
    with session() as s:
        settings_service.merge_purposes(s, {"chat": _chat()})
    with session() as s:
        public = settings_service.public_purposes(s)

    assert "sk-secret" not in json.dumps(public, ensure_ascii=False)
    assert public["chat"]["api_key_set"] is True
    assert "api_key" not in public["chat"]


def test_merge_without_api_key_keeps_the_old_one(session):
    with session() as s:
        settings_service.merge_purposes(s, {"chat": _chat()})
    with session() as s:
        # 前端拿不到明文 key，所以它回传时**不带 api_key** → 必须沿用旧值
        settings_service.merge_purposes(s, {"chat": {"mode": "api", "base_url": "https://new/v1", "model": "m2"}})
    with session() as s:
        chat = settings_service.purposes_from(settings_service.get(s))["chat"]

    assert chat["base_url"] == "https://new/v1"
    assert chat["model"] == "m2"
    assert chat["api_key"] == "sk-secret"  # ← 没被清掉


def test_merge_with_empty_string_clears_the_key(session):
    with session() as s:
        settings_service.merge_purposes(s, {"chat": _chat()})
    with session() as s:
        settings_service.merge_purposes(s, {"chat": {**_chat(), "api_key": ""}})
    with session() as s:
        chat = settings_service.purposes_from(settings_service.get(s))["chat"]

    assert chat["api_key"] == ""


def test_merge_does_not_touch_other_purposes(session):
    with session() as s:
        settings_service.merge_purposes(s, {"chat": _chat(), "tts": {"engine": "gpt-sovits"}})
    with session() as s:
        settings_service.merge_purposes(s, {"chat": {**_chat(), "model": "m9"}})
    with session() as s:
        purposes = settings_service.purposes_from(settings_service.get(s))

    assert purposes["tts"]["engine"] == "gpt-sovits"  # 没被顺手清掉
    assert purposes["chat"]["model"] == "m9"


def test_merge_keeps_the_other_keys_of_the_same_purpose(session):
    """**同一项里只传一部分字段时，别的字段不能被抹掉**（踩过一次：扫描只补了个目录，
    结果 base_url / 步数全被清空）。"""
    with session() as s:
        settings_service.merge_purposes(
            s,
            {"tts": {"base_url": "http://127.0.0.1:9880", "sample_steps": 8}},
        )
    with session() as s:
        settings_service.merge_purposes(s, {"tts": {"sovits_dir": "D:\\GPT-SoVITS"}})
    with session() as s:
        tts = settings_service.purposes_from(settings_service.get(s))["tts"]

    assert tts["sovits_dir"] == "D:\\GPT-SoVITS"
    assert tts["base_url"] == "http://127.0.0.1:9880"  # ← 没被抹掉
    assert tts["sample_steps"] == 8


def test_llm_config_prefers_purposes_chat(session):
    """老调用点（聊天 / 预热 / 定时任务）继续用 llm_config()，但要吃到新配置。"""
    with session() as s:
        settings_service.merge_purposes(s, {"chat": _chat(base_url="https://new/v1", model="m2")})
    with session() as s:
        bases_json, active = settings_service.llm_config(s)

    bases = json.loads(bases_json)
    assert active == "chat"
    assert bases[0]["name"] == "chat"
    assert bases[0]["base_url"] == "https://new/v1"
    assert bases[0]["api_key"] == "sk-secret"


def test_llm_config_falls_back_to_legacy_fields(session):
    """过渡期保险：purposes.chat 没配好时，老字段照样能用。"""
    with session() as s:
        settings_service.set_llm_bases(
            s, [{"name": "legacy", "base_url": "https://old/v1", "model": "m0", "api_key": "sk-old"}],
            active="legacy",
        )
    with session() as s:
        bases_json, active = settings_service.llm_config(s)

    assert active == "legacy"
    assert json.loads(bases_json)[0]["base_url"] == "https://old/v1"


def test_only_chat_is_required_everything_else_is_optional(session):
    """**只有 chat 是必须的**（D83）：其它用途没配 = 没这个能力，不是"出错了"。"""
    with session() as s:
        enabled = settings_service.purposes_enabled_from(settings_service.get(s))
        # v3.46（D231）：embedding 是本地 ONNX 模型，不需要地址 / 密钥 → **默认就是"有"**
        assert enabled == {
            "chat": False,
            "game_fast": False,
            "vision": False,
            "tts": False,
            "stt": False,
            "image": False,
            "embedding": True,
        }

        settings_service.merge_purposes(s, {"chat": _chat()})
    with session() as s:
        enabled = settings_service.purposes_enabled_from(settings_service.get(s))
        assert enabled["chat"] is True
        assert enabled["vision"] is True  # 默认跟随聊天（chat 能看图，D79）
        assert enabled["tts"] is False  # 没配就是没能力，不算错
        assert enabled["stt"] is False
        assert enabled["image"] is False


def test_disabled_flag_turns_a_purpose_off(session):
    with session() as s:
        settings_service.merge_purposes(s, {"chat": _chat(), "vision": {"enabled": False}})
    with session() as s:
        enabled = settings_service.purposes_enabled_from(settings_service.get(s))
        assert enabled["vision"] is False
        raw = json.dumps(settings_service.effective_purposes_from(settings_service.get(s)), ensure_ascii=False)

    with pytest.raises(LLMConfigError) as err:
        resolve_purpose_config(raw, "vision")
    assert "关掉" in str(err.value)


def test_not_configured_message_says_optional_not_error(session):
    """「没配」的报错要明确写成「可选能力」，别让用户以为坏了。"""
    with session() as s:
        settings_service.merge_purposes(s, {"chat": _chat()})
    with session() as s:
        raw = json.dumps(settings_service.effective_purposes_from(settings_service.get(s)), ensure_ascii=False)

    with pytest.raises(LLMConfigError) as err:
        resolve_purpose_config(raw, "tts")
    message = str(err.value)
    assert "可选" in message
    assert "不影响聊天" in message


def test_resolve_purpose_config_vision_follows_chat(session):
    with session() as s:
        settings_service.merge_purposes(s, {"chat": _chat(base_url="https://c/v1", model="m-c")})
    with session() as s:
        # 注意：走的是 effective_purposes_from —— 它会把"视觉默认跟随聊天"补上，
        # 数据库里其实没有 vision 这一项（用户没动过）
        raw = settings_service.effective_purposes_from(settings_service.get(s))

    cfg = resolve_purpose_config(json.dumps(raw, ensure_ascii=False), "vision")
    assert cfg.base_url == "https://c/v1"
    assert cfg.model == "m-c"
    assert "follow_chat" in cfg.name


def test_resolve_purpose_config_raises_when_nothing_configured(session):
    with session() as s:
        raw = json.dumps(settings_service.effective_purposes_from(settings_service.get(s)), ensure_ascii=False)

    with pytest.raises(LLMConfigError) as err:
        resolve_purpose_config(raw, "vision")
    assert "视觉" in str(err.value)  # 错误信息要能直接给用户看
