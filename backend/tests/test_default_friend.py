"""默认朋友 deepseek 的出厂提示词回归。"""

from backend.services.default_friend import (
    CUSTOM_INSTRUCTIONS,
    PERSONALITY,
    VOICE_EXAMPLES,
)


def test_default_friend_prompt_uses_neutral_user_address():
    assert "梦汐" not in PERSONALITY
    assert CUSTOM_INSTRUCTIONS == "（称呼：用户）"
    assert all("梦汐" not in item for item in VOICE_EXAMPLES)
