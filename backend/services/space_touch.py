"""小世界身体触区的统一标签与身体感受。"""

from __future__ import annotations


TOUCH_LABELS: dict[str, str] = {
    "head": "头",
    "ear": "耳朵",
    "tail": "尾巴",
    "hand": "手",
    "body": "身体",
    "neck": "脖子",
    "chest": "胸口",
    "waist": "腰",
    "thigh": "腿",
    "butt": "后面",
}

# 只描述身体感受，不规定她必须喜欢/发情/说什么；怎么反应仍归她的人格。
TOUCH_SENSATIONS: dict[str, str] = {
    "chest": "胸口被手掌贴着、慢慢揉时会发热、发胀，心跳和呼吸会更明显。",
    "butt": "臀部被揉捏会有钝钝的压感，也容易觉得羞。",
    "waist": "腰侧和腰窝被碰到会有点痒，身体会下意识缩一下。",
    "thigh": "大腿内侧被碰到会绷紧、发软，腿会下意识并拢。",
    "neck": "脖子被碰到会敏感地缩一下，呼吸会变浅。",
    "ear": "耳朵被碰到会痒，容易脸红或躲开。",
    "head": "被摸头时会有被安抚、被纵容的感觉。",
    "hand": "手被握住时会有被牵着、被拉住的感觉。",
    "tail": "尾巴被碰到会敏感地弹一下，身体会跟着一激灵。",
    "body": "身体被贴着会感觉到手掌的温度和压力。",
}


def touch_label(zone: str) -> str:
    return TOUCH_LABELS.get(zone, zone or "身体")


def touch_sensation(zone: str) -> str:
    return TOUCH_SENSATIONS.get(zone, "")
