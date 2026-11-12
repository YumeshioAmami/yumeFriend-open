"""对话节奏/轻重分流（v7.13 / D344）。

目标不是让模型“演短句”，而是给它一个轻聊/深聊的预算：
  · 轻聊：1~3 个短气泡，一个气泡一个念头，先接具体的事；
  · 深聊：可以展开，但最多 3 个气泡，不要写成小论文。

这是软路由 + 硬 token 上限：提示词负责姿态，max_tokens 负责兜底。
不动消息并发、SSE、工具轮和数据库里的对话结构。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Sequence


@dataclass(frozen=True)
class PacingDecision:
    register: str  # casual / deep
    max_tokens: int
    reason: str
    typo_rate: float
    # v7.15（D350）：回复形态，不只是“轻聊/深聊”
    shape: str = "two_beat"  # one_beat / two_beat / deep
    target_bubbles: int = 2
    max_bubbles: int = 3
    bubble_chars: int = 34


DEEP_WORDS = (
    "难受", "崩溃", "焦虑", "抑郁", "想哭", "哭", "绝望", "孤独", "害怕",
    "压力", "人生", "意义", "为什么", "分手", "关系", "自杀", "死",
    "痛苦", "失眠", "内耗", "虚无", "存在", "创伤", "童年", "家庭",
)

CASUAL_MAX_TOKENS = 700
DEEP_MAX_TOKENS = 1800


def _text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, dict):
        return str(value.get("content") or "")
    return str(getattr(value, "content", value) or "")


def _role(value: Any) -> str:
    if isinstance(value, dict):
        return str(value.get("role") or "")
    return str(getattr(value, "role", "") or "")


def _looks_like_question(text: str) -> bool:
    return any(mark in text for mark in ("?", "？", "吗", "呢", "怎么", "为什么", "要不要"))


def choose_pacing(user_input: str, history: Sequence[Any] = ()) -> PacingDecision:
    text = (user_input or "").strip()
    # 只看 TA 这边最近几条，不拿她自己的长回复把本轮误判成深聊。
    recent_user = [
        _text(item)
        for item in list(history)[-8:]
        if _role(item) == "user" and _text(item)
    ]
    recent_user = [item for item in recent_user if item]
    recent_avg = sum(len(item) for item in recent_user) / len(recent_user) if recent_user else 0
    deep_hit = next((word for word in DEEP_WORDS if word in text), None)
    if deep_hit:
        return PacingDecision(
            register="deep",
            max_tokens=DEEP_MAX_TOKENS,
            reason=f"命中深聊词：{deep_hit}",
            typo_rate=0.0,
            shape="deep",
            target_bubbles=3,
            max_bubbles=3,
            bubble_chars=40,
        )
    if len(text) >= 120 or recent_avg >= 90:
        return PacingDecision(
            register="deep",
            max_tokens=DEEP_MAX_TOKENS,
            reason="用户这条/最近几条明显在展开说",
            typo_rate=0.0,
            shape="deep",
            target_bubbles=3,
            max_bubbles=3,
            bubble_chars=40,
        )
    # 日常短句：目标一泡。带问句或内容稍长时给第二泡，不当场逼出第三泡。
    if len(text) <= 24 and not _looks_like_question(text):
        return PacingDecision(
            register="casual",
            max_tokens=CASUAL_MAX_TOKENS,
            reason="短句/日常闲聊",
            typo_rate=0.02,
            shape="one_beat",
            target_bubbles=1,
            max_bubbles=2,
            bubble_chars=40,
        )
    return PacingDecision(
        register="casual",
        max_tokens=CASUAL_MAX_TOKENS,
        reason="短句/日常闲聊",
        typo_rate=0.02,
        shape="two_beat",
        target_bubbles=2,
        max_bubbles=3,
        bubble_chars=40,
    )


def pacing_note(decision: PacingDecision) -> str:
    if decision.register == "casual":
        return """# 这一轮怎么回（轻聊）
· 默认 1~3 个短气泡，一个气泡只做一个动作：接一句 / 说自己的 / 问一句 / 邀请。
· 先接他刚说的具体东西，不要上来分析“他是什么状态”。
· 不要给总结、不要给三条建议、不要为了收尾强行提问。
· 短可以，但不要把对话压成一声“嗯/是/我在”的反应；能自然说下去，
  就补上你自己的判断、偏好、联想或一个小问题。没有想说的才停。
· 情绪大时可以拆成两三条短消息，但不要把反应、解释、关心各拆一段。"""
    return """# 这一轮怎么回（深聊）
· 可以展开，但最多 3 个气泡；每个气泡一个完整念头，每泡尽量不超过 40 字。
· 先接具体事实，再说判断；不要每段都先“接住”再“重新定义”。
· 不要写成咨询报告；不要把他的话翻译成你的理论。
· 说完一个判断就停，不为了显得完整补一段总结。"""


def reply_shape_note(decision: PacingDecision) -> str:
    """贴着生成点的形态提醒（D350）。

    system prompt 里的 STYLE_BLOCK/pacing_note 离生成点太远；实测 D168 的
    情绪标签提醒只有“历史之后、本轮输入之前”才稳定生效。这里沿用同一位置。
    """
    if decision.shape == "one_beat":
        return """【这一轮先按这个形态回】
· 回 1~2 句自然短话（一个气泡也可以），但不要只做一个单字/单句反应。
· 先接他刚说的具体事，再给你自己的一个完整念头：判断、偏好、联想或一个小问题。
· 短不是把话砍碎；说完一个完整念头就停，不分析他的状态、不总结、不建议。
· 只有你**真的**想知道才问，不为了收尾提问。"""
    if decision.shape == "two_beat":
        return """【这一轮先按这个形态回】
· 目标 2 个气泡，最多 3 个；第一泡接具体事，第二泡给出自己的判断、联想或一个小问题。
· 每个气泡只放一个完整念头，一般不超过 40 字；不要把第二泡写成一段解释。
· 气泡之间用单独一行的 `<<<MSG>>>` 分隔；每个气泡开头写情绪标签。
· 不总结、不列建议、不分析“他是什么状态”。
· 说完第二个念头就停；不为了显得完整硬加第三泡。"""
    return """【这一轮先按这个形态回】
· 最多 3 个气泡，每泡一般不超过 40 字；先接具体事实，再说一个判断。
· 气泡之间用单独一行的 `<<<MSG>>>` 分隔；每个气泡开头写情绪标签。
· 一个气泡一个完整念头；不要每段都“接住 → 重新定义 → 建议”。
· 不写咨询报告、不列三条建议、不把他的话翻译成你的理论。
· 说完判断就停，不为了显得完整补总结。"""


def is_casual(decision: PacingDecision) -> bool:
    return decision.register == "casual"
