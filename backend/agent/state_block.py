"""流式解析隐藏的 <state_delta> 块（V6.0-1 / D296）。

模型通常被要求在正文最前面先写：

    <state_delta>{"appraisal":"...","intent":"...",...}</state_delta>
    这里才是用户看得到的正文

但实机里模型偶尔会把状态块写在正文末尾（2026-10-25 正常聊天审计）。
因此解析器**不能只认开头**：无论状态块出现在哪里，都要从可见正文里剥掉，
并且要扣住可能被 chunk 切断的半个标签。

这个模块把它剥出来，产出：
    {"type": "state_delta", "delta": {...}}
    {"type": "content", "delta": "..."}

它不是思考链：内容只是一次结构化状态更新，不展示、不落消息。
"""

from __future__ import annotations

import json
import re
from typing import Any

from loguru import logger

STATE_OPEN = "<state_delta>"
STATE_CLOSE = "</state_delta>"
_COMPLETE_STATE_RE = re.compile(r"<state_delta>.*?</state_delta>", re.S)
_UNCLOSED_STATE_RE = re.compile(r"<state_delta>.*\Z", re.S)


def _loads_first_json(text: str) -> Any:
    raw = (text or "").strip()
    if raw.startswith("```"):
        raw = raw.split("\n", 1)[-1]
        if raw.rstrip().endswith("```"):
            raw = raw.rstrip()[:-3].rstrip()
    if not raw:
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        pass
    try:
        payload, _ = json.JSONDecoder().raw_decode(raw)
        return payload
    except json.JSONDecodeError:
        return None


class StateBlockParser:
    """增量喂 delta；状态块无论出现在开头还是末尾都剥掉，只采用第一个。"""

    def __init__(self) -> None:
        self._buf = ""
        self._mode = "text"  # text / in_block / done

    def feed(self, delta: str) -> list[dict[str, Any]]:
        if not delta:
            return []
        if self._mode == "done":
            return [{"type": "content", "delta": delta}]

        self._buf += delta
        events: list[dict[str, Any]] = []
        while True:
            if self._mode == "in_block":
                close_at = self._buf.find(STATE_CLOSE)
                if close_at < 0:
                    return events
                raw = self._buf[:close_at]
                rest = self._buf[close_at + len(STATE_CLOSE) :]
                self._buf = ""
                self._mode = "done"
                payload = _loads_first_json(raw)
                if isinstance(payload, dict):
                    events.append({"type": "state_delta", "delta": payload})
                else:
                    logger.warning("state_delta 不是合法 JSON，已丢弃：{}", raw[:160])
                rest = rest.lstrip(" \t\r\n")
                if rest.startswith("<reply>"):
                    rest = rest[len("<reply>") :].lstrip(" \t\r\n")
                if rest:
                    events.append({"type": "content", "delta": rest})
                return events

            open_at = self._buf.find(STATE_OPEN)
            if open_at >= 0:
                before = self._buf[:open_at]
                # 状态块在开头时，标签前的空白不算正文。
                if not events and before.strip() == "":
                    before = ""
                if before:
                    events.append({"type": "content", "delta": before})
                self._buf = self._buf[open_at + len(STATE_OPEN) :]
                self._mode = "in_block"
                continue

            # 没看到完整开标签时，只能把可能是标签前缀的尾巴扣住。
            hold = 0
            for size in range(min(len(STATE_OPEN) - 1, len(self._buf)), 0, -1):
                if STATE_OPEN.startswith(self._buf[-size:]):
                    hold = size
                    break
            if hold:
                out = self._buf[:-hold]
                self._buf = self._buf[-hold:]
            else:
                out = self._buf
                self._buf = ""
            if out:
                events.append({"type": "content", "delta": out})
            return events

    def flush(self) -> list[dict[str, Any]]:
        if self._mode == "in_block":
            logger.error("流结束时 state_delta 仍未闭合，已丢弃（{} 字节）", len(self._buf))
            self._buf = ""
            self._mode = "done"
            return []
        if self._mode == "text" and self._buf:
            out, self._buf = self._buf, ""
            self._mode = "done"
            return [{"type": "content", "delta": out}]
        self._mode = "done"
        return []


def strip_state_blocks(text: str) -> str:
    """落库/展示前的最后一道兜底：清掉任何残留的隐藏状态块。

    解析器正常工作时不该有残留；这里防的是格式坏掉、模型在工具轮重复输出、
    或流式边界异常造成的漏网标签。宁可少显示一行，也不让用户看到内部协议。
    """
    cleaned = _COMPLETE_STATE_RE.sub("", text or "")
    cleaned = _UNCLOSED_STATE_RE.sub("", cleaned)
    return cleaned.replace("<reply>", "").replace("</reply>", "")
