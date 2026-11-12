"""重复护栏（D104 / TASKS T2.2-7）。

为什么要有它：实测模型在**工具轮**里会把上一轮已经说过的话再说一遍
（224 条有内容的助手消息里 2 条中招，两条都调过工具）——
两轮正文拼进同一条消息，用户就在一个气泡里看到同一段话两遍。

口径：
· **只丢重复，不改写**。留下的正文一个字都不动。
· 比较前先去掉所有空白（换行、空格），但**返回的是原文**。
· 按换行切段；一段太短（<16 字）不判 —— "嗯。""汪……"这种重复要留着。
· 判定用 **12 字滑窗**：某段 ≥75% 的滑窗都在"已经说过的文本"里出现过 → 判为重复。
  这样"改几个字再说一遍"也能拦住（实测那两条正是这种）。
"""

from __future__ import annotations

import re

WINDOW = 8  # 滑窗大小（字）
MIN_CHARS = 16  # 短于这个长度的段不判（避免误杀"嗯。""汪……"）
COVERAGE = 0.75  # 被滑窗"盖住"的字占到这个比例 → 判为重复
# 这一条比上面松：只要"整段原样出现在已经说过的话里"就丢（≥8 字的整句照抄）
SUBSTRING_MIN = 8

_WS = re.compile(r"\s+")
_BLANK_RUN = re.compile(r"\n{3,}")


def normalize(text: str) -> str:
    """比较用：去掉所有空白。"""
    return _WS.sub("", text or "")


def _shingles(text: str) -> set[str]:
    if len(text) < WINDOW:
        return set()
    return {text[i : i + WINDOW] for i in range(len(text) - WINDOW + 1)}


def _covered(piece: str, seen: set[str]) -> bool:
    """这一段是不是"已经说过"了。

    按**字**算覆盖率：凡是落进某个"见过的 8 字窗口"里的字都算已说过。
    这样"改了几个字"和"两句老话拼在一起"都盖得住（实测那两条正是这两种形态）。
    """
    length = len(piece)
    if length <= WINDOW:
        return True
    covered = bytearray(length)
    for start in range(length - WINDOW + 1):
        if piece[start : start + WINDOW] in seen:
            for index in range(start, start + WINDOW):
                covered[index] = 1
    return sum(covered) / length >= COVERAGE


def strip_repeats(new_text: str, previous_text: str) -> str:
    """把 `new_text` 里"已经说过"的段丢掉（D104）。

    `previous_text` = 这条回复里**之前几轮已经发出去**的正文。
    返回可以直接发给用户的正文（可能比输入短，也可能整个变成空串）。
    """
    if not new_text or not previous_text:
        return new_text
    previous_norm = normalize(previous_text)
    seen = _shingles(previous_norm)
    if not seen and not previous_norm:
        return new_text

    kept: list[str] = []
    literal = previous_norm
    for piece in new_text.split("\n"):
        norm = normalize(piece)
        if not norm:
            kept.append(piece)
            continue
        # ① 整段原样说过（≥8 字）→ 丢
        if len(norm) >= SUBSTRING_MIN and norm in literal:
            continue
        # ② 改过几个字、但绝大部分还是老内容 → 丢
        if len(norm) >= MIN_CHARS and _covered(norm, seen):
            continue
        kept.append(piece)
        # 同一轮里再重复的也要拦（"她自己把刚说的一行又写一遍"）
        seen |= _shingles(norm)
        literal += norm

    return _BLANK_RUN.sub("\n\n", "\n".join(kept)).strip("\n")
