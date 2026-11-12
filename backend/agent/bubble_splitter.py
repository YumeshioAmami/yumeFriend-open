"""把长回复切成短气泡（v7.13 / D344）。

不替模型改内容，只在**句子边界**切；如果单句还是太长，再在逗号/顿号处切。
主要用于轻聊：一个气泡一个念头，避免“三个气泡其实都是段落”。
"""

from __future__ import annotations

import re

_SENTENCE_RE = re.compile(r"[^。！？!?；;]+[。！？!?；;]?")
_SOFT_BREAK_RE = re.compile(r"[，,、：:]+")
DEFAULT_MAX_BUBBLES = 3


def _hard_split(text: str, max_chars: int) -> list[str]:
    text = text.strip()
    if not text:
        return []
    pieces: list[str] = []
    while len(text) > max_chars:
        cut = max_chars
        # 优先在软标点后切
        for match in _SOFT_BREAK_RE.finditer(text[: max_chars + 8]):
            if 6 <= match.end() <= max_chars + 8:
                cut = match.end()
        pieces.append(text[:cut].strip())
        text = text[cut:].strip()
    if text:
        pieces.append(text)
    return pieces


def _balanced_partition(pieces: list[str], groups: int) -> list[str]:
    """把有序碎片分成最多 groups 组；不丢字，尽量让最长一组不要太长。"""
    pieces = [piece for piece in pieces if piece.strip()]
    if not pieces:
        return []
    groups = max(1, min(groups, len(pieces)))
    if groups == len(pieces):
        return pieces

    n = len(pieces)
    prefix = [0]
    for piece in pieces:
        prefix.append(prefix[-1] + len(piece))

    inf = 10**9
    # dp[i][g]：前 i 个碎片分成 g 组时，最长一组的字符数
    dp = [[inf] * (groups + 1) for _ in range(n + 1)]
    cut = [[0] * (groups + 1) for _ in range(n + 1)]
    dp[0][0] = 0
    for g in range(1, groups + 1):
        for i in range(1, n + 1):
            for j in range(g - 1, i):
                candidate = max(dp[j][g - 1], prefix[i] - prefix[j])
                if candidate < dp[i][g]:
                    dp[i][g] = candidate
                    cut[i][g] = j

    out: list[str] = []
    end = n
    for g in range(groups, 0, -1):
        start = cut[end][g]
        out.append("".join(pieces[start:end]).strip())
        end = start
    return [item for item in reversed(out) if item]


def split_bubbles(
    text: str,
    *,
    max_chars: int = 30,
    max_bubbles: int = DEFAULT_MAX_BUBBLES,
) -> list[str]:
    raw = (text or "").strip()
    if not raw:
        return []
    sentences = [part.strip() for part in _SENTENCE_RE.findall(raw) if part.strip()]
    if not sentences:
        sentences = [raw]
    pieces: list[str] = []
    for sentence in sentences:
        if len(sentence) <= max_chars:
            pieces.append(sentence)
        else:
            pieces.extend(_hard_split(sentence, max_chars))
    # 把太短的碎片并回前一个气泡，避免“嗯。”单独一条
    merged: list[str] = []
    for piece in pieces:
        if merged and len(piece) <= 6 and len(merged[-1]) + len(piece) <= max_chars:
            merged[-1] += piece
        else:
            merged.append(piece)
    if len(merged) > max_bubbles:
        merged = _balanced_partition(merged, max_bubbles)
    return [item for item in merged if item.strip()]


def split_segments(
    segments: list[str],
    *,
    max_chars: int = 30,
    max_bubbles: int = DEFAULT_MAX_BUBBLES,
) -> list[str]:
    raw = [str(segment).strip() for segment in (segments or []) if str(segment).strip()]
    if not raw:
        return []
    # 允许原样保留的唯一条件是“数量没超、每一泡也没超长”。
    # 旧版多给了 8 字容忍，结果 60 多字的整段会被当成一泡发出来，
    # 用户看到的就是“有时多气泡、有时一大段”。
    if len(raw) <= max_bubbles and all(len(item) <= max_chars for item in raw):
        return raw
    pieces: list[str] = []
    for segment in raw:
        pieces.extend(split_bubbles(segment, max_chars=max_chars, max_bubbles=99))
    return _balanced_partition(pieces, max_bubbles)
