"""把一局游戏的事件日志提炼成"摘要 + 记忆"（P3 / D250）。

**唯一依据是 game_events**；不许编（D236）。输出 JSON，解析失败就退化成纯文本摘要。
"""

from __future__ import annotations

import json
import re
from typing import Any, Iterable

from loguru import logger

SUMMARY_SYSTEM = """你是记忆整理器。下面是"你"和用户一起玩 Minecraft 的事件日志。

请**只根据日志**输出一个 JSON 对象（不要 markdown 代码块、不要解释）：

{
  "summary": "80~160 字的摘要：第三人称事实，写成『2026-10-18 和梦汐一起……』这种可检索的句子；可以带 1 句她自己的原话",
  "memories": [
    {"content": "值得长期记住的一条（里程碑 / 关系瞬间 / 约定 / 偏好 / 世界事实）", "importance": 0.6}
  ]
}

规则：
- 只能写日志里出现过的事，**不许编造**；
- 没有值得记的就给空数组；
- 不要写"系统 / 日志 / 事件 / 本轮"这些词；
- 一条记忆只写一件事，尽量带具体细节（地点、数量、谁说的）。"""


def format_events(events: Iterable[Any]) -> str:
    """把 GameEvent 行压成可读、可交给 LLM 的日志（跳过心跳 / 状态）。"""
    lines: list[str] = []
    for event in events:
        event_type = getattr(event, "type", "")
        if event_type in ("heartbeat", "status"):
            continue
        try:
            payload = json.loads(getattr(event, "payload", None) or "{}")
        except (TypeError, ValueError):
            payload = {}
        stamp = str(getattr(event, "created_at", ""))[11:16]
        if event_type == "chat":
            lines.append(f"[{stamp}] {payload.get('from', 'TA')} 说：{payload.get('text', '')}")
        elif event_type == "death":
            lines.append(f"[{stamp}] 她死了（{payload.get('cause') or '未知原因'}）")
        elif event_type == "damage":
            lines.append(f"[{stamp}] 她受到 {payload.get('amount')} 点伤害")
        elif event_type == "task_done":
            ok = "完成" if payload.get("ok") else "失败"
            result = json.dumps(payload.get("result") or {}, ensure_ascii=False)[:200]
            lines.append(f"[{stamp}] 任务 {payload.get('task_id')} {ok}：{result}")
        elif event_type == "player_near":
            lines.append(f"[{stamp}] 玩家 {payload.get('player')} 出现了")
        elif event_type == "error":
            lines.append(f"[{stamp}] 出错了：{payload.get('message')}")
    return "\n".join(lines)[-6000:]


def _parse_json(text: str) -> dict[str, Any] | None:
    cleaned = text.strip()
    cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned)
    cleaned = re.sub(r"\s*```$", "", cleaned)
    start = cleaned.find("{")
    end = cleaned.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        data = json.loads(cleaned[start : end + 1])
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


async def summarize_events(provider, transcript: str) -> tuple[str, list[dict[str, Any]]]:
    """调一次 LLM，返回 `(summary, memories)`。"""
    if not transcript.strip():
        return "", []
    messages = [
        {"role": "system", "content": SUMMARY_SYSTEM},
        {"role": "user", "content": transcript},
    ]
    chunks: list[str] = []
    async for event in provider.chat_stream(messages, temperature=0.2, max_tokens=1200):
        if event.get("type") == "content":
            chunks.append(event.get("delta") or "")
        elif event.get("type") == "done":
            break
    text = "".join(chunks).strip()
    data = _parse_json(text)
    if data is None:
        logger.warning("游戏摘要不是合法 JSON，退化成纯文本摘要")
        return text[:400], []

    summary = str(data.get("summary") or "").strip()
    memories: list[dict[str, Any]] = []
    for item in data.get("memories") or []:
        if not isinstance(item, dict):
            continue
        content = str(item.get("content") or "").strip()
        if not content:
            continue
        try:
            importance = float(item.get("importance", 0.6))
        except (TypeError, ValueError):
            importance = 0.6
        memories.append({"content": content, "importance": max(0.0, min(1.0, importance))})
    return summary, memories
