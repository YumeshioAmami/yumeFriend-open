"""D279：游戏快脑——用本地小模型做"下一步做什么"的低成本决策。

只负责**从当前状态里选一个动作**，不聊天、不写记忆、不做长期规划。
输出不是可靠的 JSON 时由调用方升级给聊天模型（DeepSeek）。
"""

from __future__ import annotations

import json
import re
import time
from typing import Any, Mapping, Sequence

from loguru import logger

# 快脑只碰这些"身体动作"；sequence / mode / patrol / follow / say 都不给它。
FAST_ACTIONS: tuple[str, ...] = (
    "mine",
    "goto",
    "attack",
    "place",
    "craft",
    "smelt",
    "equip",
    "eat",
    "drop",
    "bring",
    "escape",
)


def _extract_json(text: str) -> dict[str, Any] | None:
    raw = (text or "").strip()
    if not raw:
        return None
    raw = re.sub(r"^```(?:json)?\s*", "", raw, flags=re.IGNORECASE)
    raw = re.sub(r"\s*```$", "", raw)
    start = raw.find("{")
    end = raw.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        data = json.loads(raw[start : end + 1])
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


def _system_prompt(friend_name: str, personality: str) -> str:
    persona = (personality or "").strip().replace("\n", " ")[:360]
    return (
        f"你是{friend_name}，正在玩 Minecraft。你只负责判断“下一步做什么”，不负责聊天。\n"
        "只输出一个 JSON 对象；不要 markdown，不要解释，不要输出别的字。\n"
        f"你的人格（只影响选择风格）：{persona or '自然、谨慎、有点自己的想法'}"
    )


def _user_prompt(
    *,
    state: str,
    plan: str,
    plan_steps: Sequence[str],
    last_task: Mapping[str, Any] | None,
    allowed_actions: Sequence[str],
) -> str:
    steps = "\n".join(f"{index}. {step}" for index, step in enumerate(plan_steps, 1))
    last = ""
    if last_task:
        result = last_task.get("result") or {}
        detail = (
            result.get("error")
            or result.get("warning")
            or result.get("note")
            or ""
        )
        last = (
            f"刚做完：{last_task.get('action') or '?'} "
            f"（{'成功' if last_task.get('ok') else '失败'}）"
            + (f" — {str(detail)[:120]}" if detail else "")
        )
    return (
        f"{state}\n\n"
        f"当前计划：{plan or '（没有计划）'}\n"
        + (f"计划步骤：\n{steps}\n" if steps else "")
        + (f"{last}\n" if last else "")
        + f"可选动作：{', '.join(allowed_actions)}\n"
        "规则：\n"
        "- 只选一个动作，必须服务于当前计划；不要重复刚失败的那个动作。\n"
        "- 目标不可达 / 卡住 / 需要重新规划 / 不确定 → 输出 "
        '{"escalate": true, "reason": "..."}。\n'
        "- 不要写新计划；计划由更大的脑负责。\n"
        "- 想说话就用 say 字段（一句，最多 40 字）；不想说就留空。\n"
        '输出格式：{"action":"mine","args":{"block":"oak_log","count":8},'
        '"say":"","escalate":false}'
    )


async def decide(
    provider: Any,
    *,
    friend_name: str,
    personality: str,
    state: str,
    plan: str,
    plan_steps: Sequence[str],
    last_task: Mapping[str, Any] | None,
    allowed_actions: Sequence[str] = FAST_ACTIONS,
    max_tokens: int = 220,
) -> tuple[dict[str, Any] | None, str, dict[str, Any]]:
    """返回 `(decision, raw_text, usage)`；decision=None 表示要升级。"""
    messages = [
        {"role": "system", "content": _system_prompt(friend_name, personality)},
        {
            "role": "user",
            "content": _user_prompt(
                state=state,
                plan=plan,
                plan_steps=plan_steps,
                last_task=last_task,
                allowed_actions=allowed_actions,
            ),
        },
    ]
    started = time.monotonic()
    chunks: list[str] = []
    usage: dict[str, Any] = {}
    async for event in provider.chat_stream(
        messages,
        temperature=0.2,
        max_tokens=max_tokens,
    ):
        kind = event.get("type")
        if kind == "content":
            chunks.append(str(event.get("delta") or ""))
        elif kind == "done":
            usage = dict(event.get("usage") or {})
    raw = "".join(chunks).strip()
    data = _extract_json(raw)
    latency_ms = int((time.monotonic() - started) * 1000)
    usage["latency_ms"] = latency_ms
    if not isinstance(data, dict):
        logger.warning("游戏快脑输出不是 JSON（{}ms）：{}", latency_ms, raw[:160])
        return None, raw, usage
    if data.get("escalate"):
        logger.info("游戏快脑主动升级：{}", str(data.get("reason") or "")[:120])
        return None, raw, usage
    action = str(data.get("action") or "").strip()
    if action not in allowed_actions:
        logger.warning("游戏快脑给了不允许的动作：{!r}", action)
        return None, raw, usage
    args = data.get("args")
    if not isinstance(args, dict):
        args = {}
    say = str(data.get("say") or "").strip()[:200]
    return {"action": action, "args": args, "say": say}, raw, usage
