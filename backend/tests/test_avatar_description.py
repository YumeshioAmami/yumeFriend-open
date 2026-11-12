"""「她知道自己的样子」（D84）：生成 + 注入 + 各种"没有"的情况都不能炸。

盯三件事：
· 有描述 → system prompt 里出现 `# 你的样子`；没有 → **整段不出现**
· 没配视觉模型 / 没头像 → 安静跳过，**不抛异常**
· 生成用的提示词要求它区分"人物"和"非人物"（别把风景说成"我长这样"）
"""

from __future__ import annotations

import asyncio
import io
import json

import pytest
from PIL import Image

import backend.config as config
import backend.db as db
from backend.agent.context import APPEARANCE_HEADER, MEMORY_HEADER, build_system_prompt
from backend.models import Friend
from backend.services import avatar_description, settings_service


@pytest.fixture()
def session(tmp_path, monkeypatch):
    db_file = tmp_path / "desc.db"
    monkeypatch.setattr(config, "DB_PATH", db_file)
    monkeypatch.setattr(db, "DB_PATH", db_file)
    monkeypatch.setattr(db, "_engine", None)
    monkeypatch.setattr(db, "_session_factory", None)
    monkeypatch.setattr(config, "AVATAR_DIR", tmp_path / "avatars")
    from backend.db import Base, get_engine

    Base.metadata.create_all(get_engine())
    return db.session_scope


def _png() -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (64, 64), (200, 120, 90)).save(buf, format="PNG")
    return buf.getvalue()


class FakeVisionProvider:
    """假的多模态 provider：记下收到几条消息，回一句固定描述。"""

    def __init__(self, says: str = "银灰色的长发，眼睛是浅金色。") -> None:
        self.says = says
        self.calls: list[list[dict]] = []

    async def chat_stream(self, messages, tools=None, **kwargs):  # noqa: ANN001, ARG002
        self.calls.append(messages)
        yield {"type": "content", "delta": self.says}
        yield {"type": "done"}


def _make_friend(scope, fid="f1", with_avatar=True) -> str:
    path = config.AVATAR_DIR / f"{fid}.png"
    path.parent.mkdir(parents=True, exist_ok=True)
    if with_avatar:
        path.write_bytes(_png())
    with scope() as s:
        s.add(
            Friend(
                id=fid,
                name="阿月",
                personality="话不多",
                created_at="2026-01-01T00:00:00+00:00",
                updated_at="2026-01-01T00:00:00+00:00",
                avatar_path=f"avatars/{fid}.png" if with_avatar else None,
            )
        )
    return fid


def test_prompt_tells_it_to_distinguish_person_from_landscape():
    prompt = avatar_description.DESCRIBE_PROMPT
    assert "别硬说成" in prompt  # 非人物不许说成"我长这样"
    assert "外貌" in prompt


def test_appearance_is_injected_only_when_present(session):
    fid = _make_friend(session)
    with session() as s:
        friend = s.get(Friend, fid)
        # 没描述 → 整段不出现
        assert APPEARANCE_HEADER not in build_system_prompt(friend)

        friend.avatar_description = "银灰色的长发，眼睛是浅金色。"
        prompt = build_system_prompt(friend)
        assert APPEARANCE_HEADER in prompt
        assert "银灰色的长发" in prompt
        # 位置：人格/说话习惯之后、记忆与工具之前（档案性质，不是易变状态）
        # （v3.6 / D154 把工具块挪到了记忆之后，所以这里的右锚点跟着换）
        assert (
            prompt.index("你的说话习惯")
            < prompt.index(APPEARANCE_HEADER)
            < prompt.index(MEMORY_HEADER)
            < prompt.index("# 后台能力")
        )


def test_describe_and_store_writes_the_text(session, monkeypatch):
    fid = _make_friend(session)
    provider = FakeVisionProvider("银灰色的长发，穿着深蓝色的风衣。")

    monkeypatch.setattr(
        avatar_description, "create_provider_for_purpose", lambda *a, **k: provider
    )
    ok = asyncio.run(avatar_description.describe_and_store(fid))

    assert ok is True
    with session() as s:
        assert s.get(Friend, fid).avatar_description == "银灰色的长发，穿着深蓝色的风衣。"
    # 图确实被塞进了多模态 content（image_url）
    sent = provider.calls[0][0]["content"]
    assert any(part.get("type") == "image_url" for part in sent)


def test_describe_skips_quietly_when_no_vision_configured(session, monkeypatch):
    """没配视觉模型：**不抛异常**，只是不生成（D83/D84）。"""
    fid = _make_friend(session)

    def boom(*_a, **_k):
        from backend.llm.errors import LLMConfigError

        raise LLMConfigError("「视觉」没有启用")

    monkeypatch.setattr(avatar_description, "create_provider_for_purpose", boom)
    assert asyncio.run(avatar_description.describe_and_store(fid)) is False
    with session() as s:
        assert s.get(Friend, fid).avatar_description is None


def test_describe_skips_when_no_avatar(session):
    fid = _make_friend(session, with_avatar=False)
    assert asyncio.run(avatar_description.describe_and_store(fid)) is False


def test_provider_failure_does_not_raise(session, monkeypatch):
    fid = _make_friend(session)

    class Boom:
        async def chat_stream(self, *_a, **_k):
            raise RuntimeError("上游炸了")
            yield  # pragma: no cover

    monkeypatch.setattr(avatar_description, "create_provider_for_purpose", lambda *a, **k: Boom())
    assert asyncio.run(avatar_description.describe_and_store(fid)) is False


def test_effective_purposes_shape_is_accepted(session, monkeypatch):
    """描述任务读的是"带默认形状"的用途配置（视觉默认跟随聊天）。"""
    fid = _make_friend(session)
    seen: dict = {}

    def fake_factory(purposes_json, purpose):  # noqa: ANN001
        seen["purpose"] = purpose
        seen["purposes"] = json.loads(purposes_json)
        return FakeVisionProvider()

    monkeypatch.setattr(avatar_description, "create_provider_for_purpose", fake_factory)
    asyncio.run(avatar_description.describe_and_store(fid))

    assert seen["purpose"] == "vision"
    assert "vision" in seen["purposes"]
