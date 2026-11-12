"""流式解析多气泡分隔符（V6.0-2b / D298）。

模型想一轮分几条发时，在两条之间单独写：

    <<<MSG>>>

这个模块把它剥掉，产出：
    {"type": "content", "delta": "..."}
    {"type": "segment_break"}

分隔符本身永远不展示给用户。
"""

from __future__ import annotations

from typing import Any

SEGMENT_BREAK = "<<<MSG>>>"


class SegmentParser:
    def __init__(self) -> None:
        self._buf = ""
        self._strip_leading = False

    def feed(self, delta: str) -> list[dict[str, Any]]:
        if not delta:
            return []
        if self._strip_leading:
            delta = delta.lstrip(" \t\r\n")
            if not delta:
                return []
            self._strip_leading = False

        self._buf += delta
        events: list[dict[str, Any]] = []

        while True:
            index = self._buf.find(SEGMENT_BREAK)
            if index >= 0:
                before = self._buf[:index].rstrip(" \t\r\n")
                if before:
                    events.append({"type": "content", "delta": before})
                self._buf = self._buf[index + len(SEGMENT_BREAK) :]
                events.append({"type": "segment_break"})
                self._strip_leading = True
                self._buf = self._buf.lstrip(" \t\r\n")
                if not self._buf:
                    break
                continue

            # 末尾可能是分隔符的前半截 → 先扣住，别吐给用户
            hold = 0
            for size in range(min(len(SEGMENT_BREAK) - 1, len(self._buf)), 0, -1):
                if self._buf.endswith(SEGMENT_BREAK[:size]):
                    hold = size
                    break
            if hold:
                emit, self._buf = self._buf[:-hold], self._buf[-hold:]
            else:
                emit, self._buf = self._buf, ""
            if emit:
                events.append({"type": "content", "delta": emit})
            break

        return events

    def flush(self) -> list[dict[str, Any]]:
        if not self._buf:
            return []
        out, self._buf = self._buf, ""
        return [{"type": "content", "delta": out}]
