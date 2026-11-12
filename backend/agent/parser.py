"""流式工具块解析器（LLM协议§4.3 / architecture.md §4 ⭐ 最容易写崩的地方）。

模型在正文里按提示词协议吐工具块::

    （挑眉）行，记下了。
    <tool>{"name":"save_memory","arguments":{"content":"用户生日 5 月 20 日"}}</tool>

难点：标签会被 chunk 从中间切断（`"<to" + "ol>"`）。所以是个**带前缀缓冲的状态机**::

                     看到完整的 "<tool>"
         ┌────────┐ ───────────────────▶ ┌──────────┐
         │  TEXT  │                      │ IN_TOOL  │
         │ 正常转发│ ◀─────────────────── │  缓冲中   │
         └────────┘   看到 "</tool>"      └──────────┘
             ▲          → 解析 JSON           │
             │          → 发 tool_call       │ 超过 8KB 仍未闭合
             └──────────────────────────────┘ → 当普通文本吐出去

另外两条清理规则（LLM协议§4.1）：
· 工具块**紧邻的空行 / 空白**一并去掉（不然回复里会多出莫名其妙的大段空行）；
· JSON 坏掉 → 原样当文本输出（**不丢内容、不崩**），记 WARNING；
  到流结束仍未闭合 → 丢弃该块，记 ERROR。

产出的两种事件：
    {"type": "text",      "text": "..."}
    {"type": "tool_call", "id": "tool_1", "name": "...", "arguments": {...}}
"""

from __future__ import annotations

import json
import re
from typing import Any, Iterable

from loguru import logger

OPEN_TAG = "<tool>"
CLOSE_TAG = "</tool>"
MAX_TOOL_BYTES = 8 * 1024

# v5.0（D260）：DeepSeek 的原生工具标记（DSML）。
# 模型有时不按 `<tool>` 协议，改吐：
#   <｜｜DSML｜｜ calls>
#   <｜｜DSML｜｜ invoke name="game_do">
#   <｜｜DSML｜｜ parameter name="arguments" string="false">{...}</｜｜DSML｜｜ parameter>
#   </｜｜DSML｜｜ invoke>
#   </｜｜DSML｜｜ calls>
# 以前整块被当正文（聊天记录里全是这些字），工具没执行。这里把它也认成工具调用。
_DSML_START = re.compile(r"<[｜|]{2}\s*DSML\s*[｜|]{2}")
_DSML_END = re.compile(r"</[｜|]{2}\s*DSML\s*[｜|]{2}\s*calls\s*>")
_DSML_MARKERS = ("<｜｜DSML｜｜", "<||DSML||")

# 工具块**紧邻**的空行 / 空代码围栏，一并清掉（LLM协议§4.1 最后一行）
_TRAILING_NOISE = re.compile(r"(?:[ \t]*\n|`{3,}[a-zA-Z]*[ \t]*)+$")
_LEADING_NOISE = re.compile(r"^(?:[ \t]*\n|`{3,}[a-zA-Z]*[ \t]*\n?)+")
# buf 末尾"整行都是空白 / 围栏"的尾巴：先扣住，万一下个 chunk 就是工具块，这段就该丢
_BLANK_TAIL = re.compile(r"(?:\n[ \t`]*)+$")


def _fix_stray_quotes(text: str) -> str:
    """把"内容里带了未转义英文引号"的 JSON 尽量修回来（D241）。

    模型常把记忆内容写成 `{"content":"他说"没事"了"}` —— 内容里的引号没转义，
    `json.loads` 直接失败，于是整块被当正文吐出来（用户实测：记忆没存上、屏幕上还露着 JSON）。

    判据：字符串里的 `"`，只有后面（跳过空白）紧跟 `,` `:` `}` `]` 才算"字符串结束"；
    否则一律当内容里的引号、转义掉。修不好就原样返回（调用方仍按老办法当文本输出）。
    """
    out: list[str] = []
    in_string = False
    index = 0
    while index < len(text):
        char = text[index]
        if not in_string:
            out.append(char)
            if char == '"':
                in_string = True
            index += 1
            continue
        if char == "\\":
            out.append(char)
            if index + 1 < len(text):
                out.append(text[index + 1])
                index += 2
            else:
                index += 1
            continue
        if char == '"':
            look = index + 1
            while look < len(text) and text[look] in " \t\r\n":
                look += 1
            if look >= len(text) or text[look] in ",:}]":
                out.append(char)
                in_string = False
            else:
                out.append('\\"')
            index += 1
            continue
        out.append(char)
        index += 1
    return "".join(out)


def _close_open_brackets(text: str) -> str:
    """把“结尾括号写坏”的 JSON 补平 / 纠直（D242 + D248）。

    实测两代（都让整块当正文吐出来，用户看到 JSON 字、记忆还没存上）：

    · D242（2026-10-06）：她**漏写**最外层 `}` —— 内容很长时发生。
    · D248（2026-10-13）：她**写反了方向** —— 三条 `save_memory` 都以 `}]` 收尾
      （本该是 `}}`）：`arguments` 关了、最外层那个 `}` 写成了 `]`
      → `json.loads` 报 “Expecting ',' delimiter”。
      旧版只会“在结尾补”，补出 `}]}` 照样非法 —— 所以这次得**纠方向**，不能只补。

    做法：扫一遍、跳过字符串内部，逐字符重建。

    · 闭合符**跟栈顶配得上**（`{`…`}` / `[`…`]`）→ 原样保留；
    · **配不上**（`{` 撞上 `]`）→ 按栈顶**纠正**成正确的那个闭合符；
    · 栈已空还来闭合符（多写的）→ **丢掉**；
    · 扫完还有没关的 → 按相反顺序补上；字符串没收起尾引号也补一个。
    """
    out: list[str] = []
    stack: list[str] = []
    in_string = False
    index = 0
    while index < len(text):
        char = text[index]
        if in_string:
            out.append(char)
            if char == "\\":
                if index + 1 < len(text):
                    out.append(text[index + 1])
                    index += 2
                    continue
            elif char == '"':
                in_string = False
            index += 1
            continue
        if char == '"':
            in_string = True
            out.append(char)
        elif char in "{[":
            stack.append(char)
            out.append(char)
        elif char in "}]":
            if stack:
                opener = stack.pop()
                if (char == "}" and opener == "{") or (char == "]" and opener == "["):
                    out.append(char)
                else:
                    out.append("}" if opener == "{" else "]")  # 写反了 → 按栈顶纠正
            # 栈空还来闭合符 = 多写的，直接丢掉
        else:
            out.append(char)
        index += 1
    if in_string:
        out.append('"')  # 收尾引号也丢了 → 补一个（补错也没事，json.loads 会再判一次）
    while stack:
        out.append("}" if stack.pop() == "{" else "]")
    return "".join(out)


def _loads_first_json(text: str) -> Any:
    """解析第一个 JSON 对象；尾部多出来的字（模型偶尔会附一句解释）直接忽略。

    以前 `json.loads` 遇到 "Extra data" 就把整块当正文，工具不执行；
    这是用户看到的"她输入格式错了"的常见来源之一。
    """
    raw = (text or "").strip()
    if not raw:
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        pass
    try:
        payload, end = json.JSONDecoder().raw_decode(raw)
    except json.JSONDecodeError:
        return None
    tail = raw[end:].strip()
    if tail:
        logger.info("工具块 JSON 尾部有多余内容，已忽略（{} 字符）", len(tail))
    return payload


class ToolBlockParser:
    """增量喂 delta，产出洁净文本与工具调用。见模块 docstring 的状态机。"""

    def __init__(self) -> None:
        self._buf = ""
        self._in_tool = False
        self._in_dsml = False
        self._counter = 0
        # 刚关掉一个工具块 → 后面紧跟的空行 / 空围栏要清掉（可能在下个 chunk 才到）
        self._strip_leading = False

    @property
    def in_tool(self) -> bool:
        return self._in_tool

    def feed(self, delta: str) -> list[dict[str, Any]]:
        """喂一段增量，返回这次能确定下来的事件（可能为空列表）。"""
        if not delta:
            return []
        events: list[dict[str, Any]] = []
        self._buf += delta

        while self._buf:
            if self._in_dsml:
                match = _DSML_END.search(self._buf)
                if match is not None:
                    raw = self._buf[: match.start()]
                    self._buf = self._buf[match.end() :]
                    self._in_dsml = False
                    self._strip_leading = True
                    events.extend(self._parse_dsml(raw))
                    continue
                if len(self._buf) > MAX_TOOL_BYTES:
                    logger.warning(
                        "DSML 工具块超过 {} 字节仍未闭合，按普通文本处理",
                        MAX_TOOL_BYTES,
                    )
                    events.append({"type": "text", "text": self._buf})
                    self._buf = ""
                    self._in_dsml = False
                break

            if not self._in_tool:
                index = self._buf.find(OPEN_TAG)
                dsml = _DSML_START.search(self._buf)
                if dsml is not None and (index < 0 or dsml.start() < index):
                    head = _TRAILING_NOISE.sub("", self._buf[: dsml.start()])
                    if head:
                        events.append({"type": "text", "text": head})
                    self._buf = self._buf[dsml.end() :]
                    # 跳过开头标记后面的 " calls>"
                    self._buf = re.sub(r"^\s*calls>\s*", "", self._buf)
                    self._in_dsml = True
                    continue
                if index >= 0:
                    # 工具块前面的正文：顺手把它尾巴上的空行清掉
                    head = _TRAILING_NOISE.sub("", self._buf[:index])
                    if head:
                        events.append({"type": "text", "text": head})
                    self._buf = self._buf[index + len(OPEN_TAG) :]
                    self._in_tool = True
                    continue

                held = max(self._prefix_len(), self._dsml_prefix_len())
                if held:
                    emit, self._buf = self._buf[:-held], self._buf[-held:]
                else:
                    emit, self._buf = self._buf, ""

                # ① 尾巴上"整行空白 / 围栏"先扣住
                blank = _BLANK_TAIL.search(emit)
                if blank:
                    self._buf = emit[blank.start() :] + self._buf
                    emit = emit[: blank.start()]

                # ② 再扣住纯空白尾巴
                keep_ws = len(emit) - len(emit.rstrip())
                if keep_ws:
                    self._buf = emit[len(emit) - keep_ws :] + self._buf
                    emit = emit[: len(emit) - keep_ws]

                # ③ 刚从工具块里出来 → 清掉这一段的头
                if self._strip_leading and emit:
                    cleaned = _LEADING_NOISE.sub("", emit)
                    if cleaned:
                        emit = cleaned
                        self._strip_leading = False
                    else:
                        self._buf = emit + self._buf
                        emit = ""

                if emit:
                    events.append({"type": "text", "text": emit})
                break

            index = self._buf.find(CLOSE_TAG)
            if index >= 0:
                raw = self._buf[:index]
                # 工具块后面的空行 / 空围栏也清掉
                self._buf = _LEADING_NOISE.sub("", self._buf[index + len(CLOSE_TAG) :])
                self._in_tool = False
                self._strip_leading = True
                events.extend(self._parse(raw))
                # 同一个 chunk 里可能还有后续正文 / 第二个工具块 → 继续循环
                continue

            if len(self._buf) > MAX_TOOL_BYTES:
                logger.warning("工具块超过 {} 字节仍未闭合，按普通文本处理", MAX_TOOL_BYTES)
                events.append({"type": "text", "text": OPEN_TAG + self._buf})
                self._buf = ""
                self._in_tool = False
            break

        return events

    def flush(self) -> list[dict[str, Any]]:
        """流结束：吐出还扣着的文本；未闭合的工具块丢掉并记 ERROR。"""
        if self._in_dsml:
            logger.error("流结束时 DSML 工具块仍未闭合，已丢弃（{} 字节）", len(self._buf))
            self._buf = ""
            self._in_dsml = False
            return []
        if self._in_tool:
            logger.error("流结束时工具块仍未闭合，已丢弃（{} 字节）", len(self._buf))
            self._buf = ""
            self._in_tool = False
            return []
        if not self._buf:
            return []
        text, self._buf = _TRAILING_NOISE.sub("", self._buf), ""
        if not text:
            return []
        return [{"type": "text", "text": text}]

    def _prefix_len(self) -> int:
        """buf 末尾有多少字符可能是 "<tool>" 的开头（必须扣住不转发）。"""
        for size in range(min(len(OPEN_TAG) - 1, len(self._buf)), 0, -1):
            if self._buf.endswith(OPEN_TAG[:size]):
                return size
        return 0

    def _dsml_prefix_len(self) -> int:
        """buf 末尾有多少字符可能是 DSML 标记的开头（流式切块时先扣住）。"""
        for marker in _DSML_MARKERS:
            for size in range(min(len(marker) - 1, len(self._buf)), 0, -1):
                if self._buf.endswith(marker[:size]):
                    return size
        return 0

    def _parse_dsml(self, raw: str) -> list[dict[str, Any]]:
        """把 DeepSeek 的 DSML 工具块解析成普通 tool_call 事件。"""
        calls: list[dict[str, Any]] = []
        for invoke in re.finditer(
            r"<[｜|]{2}\s*DSML\s*[｜|]{2}\s*invoke\s+name=\"([^\"]+)\"\s*>(.*?)"
            r"</[｜|]{2}\s*DSML\s*[｜|]{2}\s*invoke\s*>",
            raw,
            re.S,
        ):
            name = invoke.group(1)
            arguments: dict[str, Any] = {}
            for param in re.finditer(
                r"<[｜|]{2}\s*DSML\s*[｜|]{2}\s*parameter\s+name=\"([^\"]+)\"[^>]*>(.*?)"
                r"</[｜|]{2}\s*DSML\s*[｜|]{2}\s*parameter\s*>",
                invoke.group(2),
                re.S,
            ):
                key = param.group(1)
                value = param.group(2).strip()
                if key == "name":
                    continue
                if key == "arguments":
                    parsed = _loads_first_json(value)
                    if parsed is None:
                        parsed = {"value": value}
                    if isinstance(parsed, dict):
                        arguments.update(parsed)
                    else:
                        arguments[key] = parsed
                else:
                    arguments[key] = value
            self._counter += 1
            calls.append(
                {
                    "type": "tool_call",
                    "id": f"dsml_{self._counter}",
                    "name": name,
                    "arguments": arguments,
                }
            )
        if not calls:
            logger.warning("DSML 工具块没解析出 invoke，按普通文本输出")
            return [{"type": "text", "text": raw}]
        return calls

    def _parse(self, raw: str) -> list[dict[str, Any]]:
        text = raw.strip()
        payload: Any = _loads_first_json(text)
        if payload is None:
            # v3.51（D241）：**先试修**——她常把内容写成 `"content":"他说"没事"了"`，
            # 内容里的英文引号没转义 → json.loads 必失败 → 以前整块当正文吐出来
            # （用户实测：记忆没存上，屏幕上还多一段 `<tool>{...}`）。
            repaired = _close_open_brackets(_fix_stray_quotes(text))
            if repaired != text:
                payload = _loads_first_json(repaired)
                if payload is not None:
                    logger.info(
                        "工具块 JSON 有点坏（引号没转义 / 括号没闭合 / 闭合写反），"
                        "已自动修好（D241/D242/D248）"
                    )
            if payload is None:
                logger.warning("工具块 JSON 解析失败，按普通文本输出：{}", text[:120])
                return [{"type": "text", "text": OPEN_TAG + raw + CLOSE_TAG}]

        if not isinstance(payload, dict) or not payload.get("name"):
            logger.warning("工具块缺少 name 字段，按普通文本输出")
            return [{"type": "text", "text": OPEN_TAG + raw + CLOSE_TAG}]

        self._counter += 1
        arguments = payload.get("arguments")
        if not isinstance(arguments, dict):
            arguments = {} if arguments is None else {"value": arguments}
        return [
            {
                "type": "tool_call",
                "id": payload.get("id") or f"tool_{self._counter}",
                "name": str(payload["name"]),
                "arguments": arguments,
            }
        ]


def normalize_native_tool_call(call: dict[str, Any]) -> dict[str, Any]:
    """把原生 function calling 的 tool_call 归一成和提示词协议一样的事件（LLM协议§4.2）。

    原生协议里 arguments 是**流式拼起来的 JSON 字符串**，这里解析成 dict；
    解析失败就原样留在 `arguments_raw`，不假装成功。
    """
    raw = call.get("arguments") or ""
    arguments: dict[str, Any] = {}
    if isinstance(raw, dict):
        arguments = raw
    else:
        parsed = _loads_first_json(raw) if raw.strip() else {}
        if parsed is None:
            logger.warning("原生工具调用参数不是合法 JSON：{}", str(raw)[:120])
            return {
                "type": "tool_call",
                "id": call.get("id") or "tool_native",
                "name": call.get("name") or "",
                "arguments": {},
                "arguments_raw": raw,
            }
        arguments = parsed if isinstance(parsed, dict) else {"value": parsed}
    return {
        "type": "tool_call",
        "id": call.get("id") or "tool_native",
        "name": call.get("name") or "",
        "arguments": arguments,
    }


# ---------- v3.31（D216）：回复开头的"时间戳"要剥掉 ----------

# 历史消息每条都带本地时间戳（`[MM-DD HH:MM]` / 跨年 `[YYYY-MM-DD HH:MM]`）→ 模型可能学着
# 在正文里也写一个（D113 情绪标签就是这么漏的）。这是**显示层兜底**：只剥回复**最开头**、
# 且必须长得像时间戳；`[1]` / `[图片#12]` / 正文里引用到的时间，一个字都不动。
_STAMP_FULL = re.compile(
    r"^(?:\[\d{2,4}-\d{2}(?:-\d{2})? \d{1,2}:\d{2}\]|\[\d{1,2}:\d{2}\])\s*"
)
# "还有可能拼成时间戳"的前缀（用来决定要不要先扣着等下一块）
_STAMP_PARTIAL = re.compile(
    r"^\[\d{0,4}(?:-\d{0,2}(?:-\d{0,2})?)?(?: \d{0,2}(?::\d{0,2})?)?\]?[ \t]*$"
)


class LeadingStampStripper:
    """把**回复开头**的本地时间戳剥掉（D216）。

    为什么做成有状态的：时间戳可能被 chunk 切开（`[09-28 21` + `:03] 在的`）。
    只有"像时间戳前缀"的才先扣着（`[09`、`[2026-`…）；不像的（`[图`、正文）立刻原样放行，
    一个字的正文都不耽误。整条回复只剥开头这一次。
    """

    def __init__(self) -> None:
        self._buf = ""
        self._done = False

    def feed(self, delta: str) -> str:
        if self._done:
            return delta
        if not delta:
            return ""
        self._buf += delta
        head = self._buf.lstrip(" \t\n")
        match = _STAMP_FULL.match(head)
        if match:
            self._done = True
            self._buf = ""
            return head[match.end() :]
        if _STAMP_PARTIAL.match(head):
            return ""  # 还有可能是时间戳 → 先扣着等下一块
        self._done = True
        out, self._buf = self._buf, ""
        return out

    def flush(self) -> str:
        """流结束：把扣着的吐出来；如果扣着的正好是完整时间戳，也剥掉。"""
        if self._done or not self._buf:
            self._done = True
            self._buf = ""
            return ""
        head = self._buf.lstrip(" \t\n")
        match = _STAMP_FULL.match(head)
        out = head[match.end() :] if match else self._buf
        self._buf = ""
        self._done = True
        return out


_LINE_STAMP_FULL = re.compile(
    r"^(?:\[\d{2,4}-\d{2}(?:-\d{2})? \d{1,2}:\d{2}\]|\[\d{1,2}:\d{2}\])[ \t]*"
)
_LINE_STAMP_PARTIAL = re.compile(
    r"^(?:\[\d{0,4}(?:-\d{0,2}(?:-\d{0,2})?)?(?: \d{0,2}(?::\d{0,2})?)?\]?"
    r"|\[\d{0,2}(?::\d{0,2})?\]?)[ \t]*$"
)


class LineStampStripper:
    """剥掉每个气泡/行首的时间戳（D216 的续：模型会逐段复制）。"""

    def __init__(self) -> None:
        self._buf = ""
        self._at_line_start = True

    def feed(self, delta: str) -> str:
        self._buf += delta or ""
        out: list[str] = []
        while self._buf:
            if self._at_line_start:
                stripped = self._buf.lstrip(" \t")
                if stripped.startswith("["):
                    match = _LINE_STAMP_FULL.match(stripped)
                    if match:
                        self._buf = stripped[match.end() :]
                        self._at_line_start = False
                        continue
                    if _LINE_STAMP_PARTIAL.match(stripped):
                        self._buf = stripped
                        return "".join(out)
                self._at_line_start = False
            newline = self._buf.find("\n")
            if newline < 0:
                out.append(self._buf)
                self._buf = ""
                break
            out.append(self._buf[: newline + 1])
            self._buf = self._buf[newline + 1 :]
            self._at_line_start = True
        return "".join(out)

    def flush(self) -> str:
        out = self._buf
        if self._at_line_start:
            stripped = out.lstrip(" \t")
            match = _LINE_STAMP_FULL.match(stripped)
            if match:
                out = stripped[match.end() :]
        self._buf = ""
        return out

# ---------- v2.2（D102）：表情标记 `[立绘:高兴]` ----------

# v2.2（D113）：模型（实测）根本不用 `[立绘:x]`，它自己写成 `**生气**：` 这种"剧本式"标签。
# 与其跟它犟，不如**两种都认** —— 认出来就摘掉、算成这一段的情绪。
MOOD_WORDS = (
    "正常", "平静", "高兴", "开心", "兴奋", "惊喜", "惊讶", "害羞", "羞耻", "心动",
    "生气", "愤怒", "厌恶", "不爽", "伤心", "难过", "无奈", "担心", "紧张", "慌张",
    "害怕", "疑惑", "困惑", "自信", "认真", "调皮", "得意", "尴尬",
    # v3.2（D147）：真实聊天记录里出现、原来漏掉的几个（用户看到的"没被摘掉"就是它们）
    "顺从", "乞求", "关心", "羞惧",
    # v3.6（D154 同批）：从她 2491 条真实消息里数出来的、她会用的情绪标记
    # （【正经】15 次、【好奇】11、【思考】6、【欢呼】5、【激动】2、【期待】2…）
    "正经", "好奇", "思考", "沉思", "欢呼", "激动", "期待", "恍然大悟", "恍然",
    "轻快", "满足", "温柔", "感动", "委屈", "撒娇", "叹气", "偷笑", "坏笑",
    "心虚", "释然", "纠结", "嘀咕", "愣住",
    # v3.13（D181）：LingChat 那两位新朋友（钦灵 / 风雪）真实会写的词 —— 它们**不是**立绘文件名，
    # 但认出来才能"摘掉 + 落到近义立绘/动作"（见前端 MOOD_ALIAS），不然就会漏在正文里。
    "冷淡", "盯着", "无语", "小声嘀咕",
    # v3.51（D241）：从**真实记录里数出来**的、她实际在用但不在名单里的词 ——
    # 不在名单 = 摘不掉 = 漏在正文里（实测：平和 71 次！调侃 10 / 嘟囔 5 / 小声 4 / 沮丧 2）。
    "平和", "调侃", "嘟囔", "小声", "沮丧",
    # v7.18.1（D370）：归宿型汐音实际写出的近义情绪 —— 不在名单就会把【软】【笑】漏进正文。
    "软", "柔软", "笑", "轻笑", "宠溺",
)
# v3.1（用户实测）：DeepSeek 爱写成 `[调皮]`（方括号 + 情绪词，**没有**"立绘:"前缀）
# → 两种都认。注意：**不能**退化成"见方括号就摘"（`[1]`、`[图片#12]` 那些是正文！）
MOOD_TAG = re.compile(
    # v3.6（D156）：三种形态后面**都可以跟一个冒号** —— 模型爱写 `[开心]：xxx`，
    # 以前只摘掉 `[开心]`、把冒号留下，用户看到的回复就是"：xxx"（实测阿月 561 条里 143 条这样）。
    # v3.7：**标签和冒号之间还可能空一行**（`[心动]\n\n：我也想你`）—— 所以允许最多两个换行。
    # v3.8：**冒号还可能跑在标签前面**（`：[得意]本天才少女上线` —— 实测汐音整条都是这个写法）。
    # 所以前面也允许一个可选冒号。⚠️ 只在"情绪标签"这三个分支上加，`[1]`/`[图片#12]` 这些正文不受影响。
    r"[:：]?[ \t]*\[[ \t]*立绘[ \t]*[:：][ \t]*([^\]\s]{1,24})[ \t]*\][ \t]*(?:\n[ \t]*){0,2}[:：]?"
    r"|[:：]?[ \t]*\[[ \t]*(" + "|".join(MOOD_WORDS) + r")[ \t]*\][ \t]*(?:\n[ \t]*){0,2}[:：]?"
    # v3.6（D154）：LingChat 那边她习惯写 `【情绪】`（全角方括号）—— 也认
    r"|[:：]?[ \t]*【[ \t]*(" + "|".join(MOOD_WORDS) + r")[ \t]*】[ \t]*(?:\n[ \t]*){0,2}[:：]?"
)
# v7.18.1（D370）：DeepSeek 在“剧本式”提示下还会写 `<状态：蹙眉>` / `<情绪：软下来>`。
# 只认行首、只认“状态/情绪”两个词，避免误伤普通 HTML / 正文尖括号。
ANGLE_MOOD_TAG = re.compile(
    r"(?m)^[ \t]*<(?:状态|情绪)[ \t]*[:：][ \t]*([^>\n]{1,32})>[ \t]*(?:\n[ \t]*){0,2}[:：]?"
)
# v7.18.1（D370）：模型还会自创 `【心疼】` / `【软下来】` / `【轻声】`。
# 只兜底“行首 + 全角括号 + 短中文（可带常用标点）”，不会碰 `【图片#12】`。
GENERIC_FULLWIDTH_MOOD_TAG = re.compile(
    r"(?m)^[ \t]*[:：]?[ \t]*[【〔〖「『［｛《〈﹝﹙]"
    r"[ \t]*([\u3400-\u9fff][\u3400-\u9fff，,、· \t]{0,11})[ \t]*"
    r"[】〕〗」』）］｝》〉﹞﹚][ \t]*(?:\n[ \t]*){0,2}[:：]?"
)
# v7.18.1（D370）：还会写成 `<开心>正文</开心>` 这种成对包裹。
GENERIC_ANGLE_MOOD_TAG = re.compile(
    r"(?m)^[ \t]*<([\u3400-\u9fff]{1,12})>[ \t]*(?:\n[ \t]*){0,2}[:：]?"
)
ANGLE_MOOD_CLOSE = re.compile(
    r"</[\u3400-\u9fff]{1,12}>[ \t]*(?:\n[ \t]*){0,2}"
)
# 半截标签的样子（会被 chunk 切开）→ 先扣在缓冲区里，等下一段
_MOOD_PARTIAL = ("[", "[立", "[立绘", "[立绘:", "[立绘：")

_MOOD_ALIASES = (
    ("蹙眉", "担心"),
    ("凑近", "心动"),
    ("心疼", "担心"),
    ("轻声", "小声"),
    ("轻一点", "小声"),
    ("靠近", "心动"),
    ("靠过来", "心动"),
    ("困", "无奈"),
    ("安心", "平静"),
    ("有点慌", "慌张"),
    ("轻轻", "小声"),
    ("软下来", "软"),
)

_PAREN_LABELS = tuple(
    dict.fromkeys(
        (
            *MOOD_WORDS,
            *(cue for cue, _mood in _MOOD_ALIASES),
            "软下来",
        )
    )
)
PAREN_MOOD_TAG = re.compile(
    r"(?m)^[ \t]*[:：]?[ \t]*（[ \t]*("
    + "|".join(re.escape(word) for word in _PAREN_LABELS)
    + r")[ \t]*）[ \t]*(?:\n[ \t]*){0,2}[:：]?"
)

_OPEN_TO_CLOSE = {
    "[": "]",
    "【": "】",
    "〔": "〕",
    "〖": "〗",
    "「": "」",
    "『": "』",
    "（": "）",
    "［": "］",
    "｛": "｝",
    "《": "》",
    "〈": "〉",
    "﹝": "﹞",
    "﹙": "﹚",
    "<": ">",
}


def _first_mood_group(match: re.Match) -> str:
    for group in match.groups():
        if group:
            return str(group).strip()
    return ""


def _normalize_mood(raw: str, allowed_moods: Iterable[str] = ()) -> str:
    """把模型写的长描述归一成情绪白名单里的词。"""
    text = (raw or "").strip()
    allowed = tuple(str(item).strip() for item in allowed_moods if str(item).strip())
    if text in allowed:
        return text
    if text in MOOD_WORDS:
        return text
    for word in sorted(MOOD_WORDS, key=len, reverse=True):
        if word in text:
            return word
    for cue, mood in _MOOD_ALIASES:
        if cue in text:
            return mood
    # 模型自创的词可能无穷无尽；落一个稳的兜底，别把未知值塞进前端。
    return "平静"


def _looks_like_mood_partial(tail: str, mood_words: Iterable[str] = MOOD_WORDS) -> bool:
    """`[` / `【` 开头、还没等到收尾括号的尾巴，像不像一个**情绪标签**的开头？（D147 / D154）

    为什么要它：`[调皮]` 会被 chunk 切成 `[调` + `皮]`。要是 `[调` 直接吐给用户，
    屏幕上就会闪出"皮]"这种残渣。**只扣"确实是标签开头"的**——
    `[1]`、`[图片#12]` 这些正文一个字都不许耽误。
    """
    if len(tail) > 40:
        return False
    for opener, closer in _OPEN_TO_CLOSE.items():
        if opener == "<" or not tail.startswith(opener):
            continue
        inner = tail[len(opener) :].strip(" \t")
        if inner == "":
            return True  # 就一个孤零零的 `[` / `【` → 等下一块
        if "立绘".startswith(inner) or inner.startswith("立绘"):
            return True  # `[立` / `[立绘:高兴`
        if opener == "（":
            return any(word.startswith(inner) for word in _PAREN_LABELS)
        if opener != "[" and inner and len(inner) <= 12:
            return all(
                "\u3400" <= char <= "\u9fff" or char in "，,、· \t"
                for char in inner
            )
        return any(word.startswith(inner) for word in mood_words)
    if tail.startswith("<"):
        inner = tail[1:].strip(" \t")
        probe = inner[1:].strip(" \t") if inner.startswith("/") else inner
        if probe and len(probe) <= 12 and all(
            "\u3400" <= char <= "\u9fff" for char in probe
        ):
            return True
        return (
            "状态".startswith(inner)
            or "情绪".startswith(inner)
            or inner.startswith("状态")
            or inner.startswith("情绪")
        )
    return False


# 行首的粗体标签：`**生气**：`
# ⚠️ 2026-10-02（用户报的 bug）：这里以前**不锚行首、冒号还是可选的** —— 于是正文里
# 任何加粗的情绪词（"他**开心**得像个孩子"）都被当成标签吃掉，正文缺字。
# 现在的规矩：**只摘"标签形态"**（行首 + 粗体 + 带动词的那个冒号），
# 单独的 `开心`、句中的 `**开心**` 一律当正文留着。
MOOD_LABEL_BOLD = re.compile(
    r"(?m)^[ \t]*[:：]?[ \t]*\*{1,2}[ \t]*("
    + "|".join(MOOD_WORDS)
    + r")[ \t]*\*{1,2}[ \t]*[:：][ \t]*"
)
# 行首的 `**生气**：` / `生气:` / `*生气*：`
MOOD_LABEL = re.compile(
    r"(?m)^[ \t]*\*{0,2}[ \t]*(" + "|".join(MOOD_WORDS) + r")[ \t]*\*{0,2}[ \t]*[:：][ \t]*"
)

# DeepSeek 偶尔会把情绪写成 Markdown 标题：`## 疑惑` / `### 害羞：`
MOOD_LABEL_HEADING = re.compile(
    r"(?m)^[ \t]*#{1,6}[ \t]*("
    + "|".join(MOOD_WORDS)
    + r")[ \t]*(?:[:：][ \t]*)?(?:\n[ \t]*)?"
)

# v3.8（D159 的**后半边**）：行首只剩一个孤零零的冒号 —— 那是"冒号跑在标签前面"的渣
# （`：[得意]正文`，D159 记过的第三种形态）。摘掉标签后剩一个 `：`，读起来很出戏。
# 前端 `stripMoodMarks` 早就补了这道兜底，**后端漏了** —— 而"朋友列表的预览"正是后端算的
# （`friend_service` 调 strip_mood_marks）→ 列表上会看到"：在的…"（实测）。
# 只在这一个形状上下手：**行首 + 冒号 + 紧跟中文**（`)` 这种表情、英文冒号、`1:2` 都不动）。
_ORPHAN_LEAD_COLON = re.compile(r"(^|\n)[ \t]*[:：](?=[\u3400-\u9fff])")


def strip_mood_marks(text: str) -> str:
    """把情绪标签从文字里摘掉（D113）—— 列表预览、其它地方要"干净文字"时用它。"""
    cleaned = MOOD_TAG.sub("", text or "")
    cleaned = ANGLE_MOOD_TAG.sub("", cleaned)
    cleaned = GENERIC_FULLWIDTH_MOOD_TAG.sub("", cleaned)
    cleaned = PAREN_MOOD_TAG.sub("", cleaned)
    cleaned = GENERIC_ANGLE_MOOD_TAG.sub("", cleaned)
    cleaned = ANGLE_MOOD_CLOSE.sub("", cleaned)
    cleaned = MOOD_LABEL.sub("", cleaned)
    cleaned = MOOD_LABEL_BOLD.sub("", cleaned)
    cleaned = MOOD_LABEL_HEADING.sub("", cleaned)
    return _ORPHAN_LEAD_COLON.sub(r"\1", cleaned)


class MoodTagParser:
    """把正文里的 `[立绘:高兴]` **摘出来**（它可能被切在两个 chunk 之间）。

    · `feed(delta)` → 返回**可以给用户看**的文本（标签已经拿掉）
    · `flush()` → 流结束时把尾巴吐出来
    · `mood` → 摘到的情绪名（没有就是 None）

    为什么要它：标签要是漏进聊天，用户就会看到 `[立绘:高兴]` 这种字。
    """

    def __init__(self, allowed_moods: Iterable[str] = ()) -> None:
        self._buffer = ""
        self.mood: str | None = None
        # D112：按顺序记下"这一刻摘到了什么情绪"（每条回复可能有好几个）
        self.moods: list[str] = []
        self._pending: list[dict] = []
        # D115：已经吐给前端的干净字数 —— 情绪记在"这一刻的偏移"上，
        # 前端才能把"第 N 段的情绪"精确对上第 N 段（老写法按顺序数，段落没标签就会错位）
        self.emitted = 0
        self._mood_words = tuple(
            dict.fromkeys(
                [
                    *MOOD_WORDS,
                    *(
                        str(item).strip()
                        for item in allowed_moods
                        if str(item).strip()
                    ),
                ]
            )
        )
        extras = tuple(word for word in self._mood_words if word not in MOOD_WORDS)
        self._extra_words = extras
        self._extra_mood_tag = (
            re.compile(
                r"[:：]?[ \t]*\[[ \t]*("
                + "|".join(re.escape(word) for word in sorted(extras, key=len, reverse=True))
                + r")[ \t]*\][ \t]*(?:\n[ \t]*){0,2}[:：]?"
                r"|[:：]?[ \t]*【[ \t]*("
                + "|".join(re.escape(word) for word in sorted(extras, key=len, reverse=True))
                + r")[ \t]*】[ \t]*(?:\n[ \t]*){0,2}[:：]?"
            )
            if extras
            else None
        )
        extra_alt = (
            "|".join(re.escape(word) for word in sorted(extras, key=len, reverse=True))
            if extras
            else None
        )
        self._extra_label_bold = (
            re.compile(
                r"(?m)^[ \t]*[:：]?[ \t]*\*{1,2}[ \t]*("
                + extra_alt
                + r")[ \t]*\*{1,2}[ \t]*[:：][ \t]*"
            )
            if extra_alt
            else None
        )
        self._extra_label = (
            re.compile(
                r"(?m)^[ \t]*\*{0,2}[ \t]*("
                + extra_alt
                + r")[ \t]*\*{0,2}[ \t]*[:：][ \t]*"
            )
            if extra_alt
            else None
        )
        self._extra_heading = (
            re.compile(
                r"(?m)^[ \t]*#{1,6}[ \t]*("
                + extra_alt
                + r")[ \t]*(?:[:：][ \t]*)?(?:\n[ \t]*)?"
            )
            if extra_alt
            else None
        )

    def _find_mood_tag(self) -> re.Match | None:
        match = MOOD_TAG.search(self._buffer)
        if match is None and self._extra_mood_tag is not None:
            match = self._extra_mood_tag.search(self._buffer)
        return match

    def _find_label(self) -> re.Match | None:
        match = MOOD_LABEL.search(self._buffer)
        if match is None and self._extra_label is not None:
            match = self._extra_label.search(self._buffer)
        return match

    def _find_bold_label(self) -> re.Match | None:
        match = MOOD_LABEL_BOLD.search(self._buffer)
        if match is None and self._extra_label_bold is not None:
            match = self._extra_label_bold.search(self._buffer)
        return match

    def _find_heading(self) -> re.Match | None:
        match = MOOD_LABEL_HEADING.search(self._buffer)
        if match is None and self._extra_heading is not None:
            match = self._extra_heading.search(self._buffer)
        return match

    def take_new_moods(self) -> list[dict]:
        """把**上次调用之后**新摘到的情绪吐出来（带"在干净文本里的偏移"）。"""
        out, self._pending = self._pending, []
        return out

    def feed(self, delta: str) -> str:
        self._buffer += delta or ""
        visible: list[str] = []
        # 同一块里可能连续出现多个 `**情绪**：`。`at` 必须算成
        # “已经可见的正文 + 本块里标签前面还没吐出去的正文 + 标签在当前缓冲区的起点”，
        # 否则多个标签会全落在同一个偏移上，按段对齐时后几句就都变成同一个情绪。
        def remember(mood: str, start: int) -> None:
            prefix = self.emitted + sum(len(part) for part in visible)
            self._remember(mood, prefix + start)

        while True:
            match = self._find_mood_tag()
            if match is not None:
                # v3.6（D156）：**标签正好卡在缓冲区末尾时先扣住** ——
                # 下一个 chunk 可能是冒号（`[开心]` + `：xxx`），现在摘了的话冒号会漏出去。
                # （已经带到冒号了就不用等，直接处理。）
                if match.end() == len(self._buffer) and not match.group(0).rstrip().endswith(
                    ("：", ":")
                ):
                    visible.append(self._buffer[: match.start()])
                    self._buffer = self._buffer[match.start():]
                    text = "".join(visible)
                    self.emitted += len(text)
                    return text
                # group(1) = `[立绘:x]` 的 x；group(2) = `[情绪词]`（D147）；group(3) = `【情绪词】`（D154）
                remember(_first_mood_group(match).strip(), match.start())
                visible.append(self._buffer[: match.start()])
                self._buffer = self._buffer[match.end() :]
                continue
            # D113：模型爱写的 `**生气**：` —— 也摘掉（它会把情绪当"剧本标签"写）
            label = self._find_label()
            if label is None:
                # 行中的 `**生气**：`（粗体包住的，不会误伤普通正文）
                label = self._find_bold_label()
            if label is not None:
                remember(label.group(1).strip(), label.start())
                visible.append(self._buffer[: label.start()])
                self._buffer = self._buffer[label.end() :]
                continue
            heading = self._find_heading()
            if heading is not None:
                remember(heading.group(1).strip(), heading.start())
                visible.append(self._buffer[: heading.start()])
                self._buffer = self._buffer[heading.end() :]
                continue
            angle = ANGLE_MOOD_TAG.search(self._buffer)
            if angle is not None:
                remember(angle.group(1).strip(), angle.start())
                visible.append(self._buffer[: angle.start()])
                self._buffer = self._buffer[angle.end() :]
                continue
            generic = GENERIC_FULLWIDTH_MOOD_TAG.search(self._buffer)
            if generic is not None:
                remember(_first_mood_group(generic), generic.start())
                visible.append(self._buffer[: generic.start()])
                self._buffer = self._buffer[generic.end() :]
                continue
            paren = PAREN_MOOD_TAG.search(self._buffer)
            if paren is not None:
                remember(paren.group(1).strip(), paren.start())
                visible.append(self._buffer[: paren.start()])
                self._buffer = self._buffer[paren.end() :]
                continue
            generic_angle = GENERIC_ANGLE_MOOD_TAG.search(self._buffer)
            if generic_angle is not None:
                remember(generic_angle.group(1).strip(), generic_angle.start())
                visible.append(self._buffer[: generic_angle.start()])
                self._buffer = self._buffer[generic_angle.end() :]
                continue
            closing = ANGLE_MOOD_CLOSE.search(self._buffer)
            if closing is not None:
                visible.append(self._buffer[: closing.start()])
                self._buffer = self._buffer[closing.end() :]
                continue
            break
        index = max(self._buffer.rfind(opener) for opener in _OPEN_TO_CLOSE)
        if index >= 0:
            tail = self._buffer[index:]
            closer = _OPEN_TO_CLOSE[tail[0]]
            if closer not in tail and (
                tail in _MOOD_PARTIAL or _looks_like_mood_partial(tail, self._mood_words)
            ):
                visible.append(self._buffer[:index])
                self._buffer = tail
                text = "".join(visible)
                self.emitted += len(text)
                return text
        # D113：`**兴奋**：` 也可能被 chunk 切开（先来 `**兴`）——
        # 尾巴像是"某个情绪词的前半截"就先扣着，等冒号到了再一起摘；
        # 不满足就照常吐出去（普通正文一个字都不该被耽误）。
        tail = self._buffer
        if "\n" not in tail and len(tail.strip()) <= 20:
            core = tail.strip().strip("* \t")
            # v3.6：**光秃秃的单字不扣**。白名单里有「好奇」之后，单字"好"会被当成它的前半截扣住，
            # 于是"好"这种最常见的回复被延迟到下一块（实测把两条 content 合并成了一条）。
            # 但 `**兴`（带星号、准备拼成 `**兴奋**：`）这种半截**还是要扣**。
            looks_bold = "*" in tail
            if core == "" or (
                (len(core) >= 2 or looks_bold)
                and any(word.startswith(core) for word in self._mood_words)
            ):
                text = "".join(visible)
                self.emitted += len(text)
                return text
        # `## 疑` / `### 害` 这种切开的标题标签也先扣住，等它拼完整。
        heading_tail = re.search(r"(?m)^[ \t]*#{1,6}[ \t]*([^\n]*)$", self._buffer)
        if heading_tail is not None:
            core = heading_tail.group(1).strip().strip("* ：:")
            if core == "" or any(word.startswith(core) for word in self._mood_words):
                visible.append(self._buffer[: heading_tail.start()])
                self._buffer = self._buffer[heading_tail.start() :]
                text = "".join(visible)
                self.emitted += len(text)
                return text
        visible.append(self._buffer)
        self._buffer = ""
        text = "".join(visible)
        self.emitted += len(text)
        return text

    def _remember(self, mood: str, at: int | None = None) -> None:
        normalized = _normalize_mood(mood, self._mood_words)
        self.mood = normalized
        self.moods.append(normalized)
        self._pending.append(
            {"mood": normalized, "at": self.emitted if at is None else int(at)}
        )

    def flush(self) -> str:
        """流结束：把缓冲区里**已经完整**的情绪标签也摘掉（记下情绪），剩下的原样吐出来。

        v3.6（D156）：feed 会把"正好卡在末尾的标签"扣住等冒号，所以收尾要负责把它摘干净
        —— 否则一条以 `[开心]` 结尾的回复会把标签原样显示出来。
        """
        visible: list[str] = []

        def remember(mood: str, start: int) -> None:
            prefix = self.emitted + sum(len(part) for part in visible)
            self._remember(mood, prefix + start)

        while True:
            match = self._find_mood_tag()
            if match is not None:
                remember(_first_mood_group(match).strip(), match.start())
                visible.append(self._buffer[: match.start()])
                self._buffer = self._buffer[match.end() :]
                continue
            angle = ANGLE_MOOD_TAG.search(self._buffer)
            if angle is not None:
                remember(angle.group(1).strip(), angle.start())
                visible.append(self._buffer[: angle.start()])
                self._buffer = self._buffer[angle.end() :]
                continue
            heading = self._find_heading()
            if heading is not None:
                remember(heading.group(1).strip(), heading.start())
                visible.append(self._buffer[: heading.start()])
                self._buffer = self._buffer[heading.end() :]
                continue
            generic = GENERIC_FULLWIDTH_MOOD_TAG.search(self._buffer)
            if generic is not None:
                remember(_first_mood_group(generic), generic.start())
                visible.append(self._buffer[: generic.start()])
                self._buffer = self._buffer[generic.end() :]
                continue
            paren = PAREN_MOOD_TAG.search(self._buffer)
            if paren is not None:
                remember(paren.group(1).strip(), paren.start())
                visible.append(self._buffer[: paren.start()])
                self._buffer = self._buffer[paren.end() :]
                continue
            generic_angle = GENERIC_ANGLE_MOOD_TAG.search(self._buffer)
            if generic_angle is not None:
                remember(generic_angle.group(1).strip(), generic_angle.start())
                visible.append(self._buffer[: generic_angle.start()])
                self._buffer = self._buffer[generic_angle.end() :]
                continue
            closing = ANGLE_MOOD_CLOSE.search(self._buffer)
            if closing is not None:
                visible.append(self._buffer[: closing.start()])
                self._buffer = self._buffer[closing.end() :]
                continue
            break
        visible.append(self._buffer)
        self._buffer = ""
        text = "".join(visible)
        self.emitted += len(text)
        return text
