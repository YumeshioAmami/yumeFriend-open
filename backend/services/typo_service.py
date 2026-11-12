"""低概率中文手滑（v7.13 / D344）。

只做很保守的同音/近音替换，最多一处；不碰数字、URL、代码、工具结果。
目的是打破“完美文本”的机器感，不是把话写错。
"""

from __future__ import annotations

import random
import re

_PAIRS = {
    "在": "再",
    "再": "在",
    "的": "得",
    "得": "的",
    "地": "的",
    "吧": "罢",
    "吗": "嘛",
    "那": "哪",
    "哪": "那",
    "做": "作",
    "作": "做",
    "象": "像",
    "像": "象",
    "以": "已",
    "已": "以",
}
_SKIP_RE = re.compile(r"https?://|\d|[A-Za-z_]{3,}|<[^>]+>")


def maybe_typo(text: str, *, rate: float = 0.02, rng: random.Random | None = None) -> str:
    raw = text or ""
    if not raw or rate <= 0 or len(raw) < 8 or _SKIP_RE.search(raw):
        return raw
    rng = rng or random
    candidates = [index for index, char in enumerate(raw) if char in _PAIRS]
    if not candidates:
        return raw
    if rng.random() >= rate:
        return raw
    index = rng.choice(candidates)
    return raw[:index] + _PAIRS[raw[index]] + raw[index + 1 :]
