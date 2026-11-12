"""小世界 v1 的角色反应档案（D372）。

这份档案不是新人格，也不是系统替朋友做价值判断；它只是把用户写的人格
压缩成小世界实时交互能用的短句/倾向。生成一次后由前端按 personality 更新时间缓存。
LLM 不可用时回退到通用默认档案，不能因为这份可选能力坏掉就让人物不出现。
"""

from __future__ import annotations

import json
import re
from typing import Any

from loguru import logger

from backend.models import Friend

DEFAULT_PROFILE: dict[str, Any] = {
    "version": 3,
    "attention": {"curiosity": 0.55, "patience": 0.55, "initiative": 0.45},
    "idle": [
        {"kind": "window", "text": "趴在窗边看外面的光"},
        {"kind": "read", "text": "翻一本旧书，看到一半又走神了"},
        {"kind": "tail", "text": "低头看自己的尾巴"},
        {"kind": "phone", "text": "拿着手机慢慢刷，偶尔笑一下"},
        {"kind": "stretch", "text": "伸了个懒腰，活动了一下肩膀"},
    ],
    "notice": {
        "lines": ["嗯？你什么时候来的。", "……我感觉到你在看我了。"],
        "expression": "relaxed",
    },
    "call": {
        "lines": ["我在。怎么啦？", "嗯，我听着呢。"],
        "expression": "happy",
    },
    "zones": {
        "head": {
            "lines": ["唔……头发要乱了。", "别揉啦，我又不是小孩子。"],
            "expression": "happy",
        },
        "ear": {
            "lines": ["耳朵不能乱碰啦。", "你手怎么这么欠呀。"],
            "expression": "surprised",
        },
        "tail": {
            "lines": ["尾巴不是给你拽的！", "别、别碰那里……"],
            "expression": "surprised",
        },
        "hand": {
            "lines": ["手先给我，别乱摸。", "你抓这么紧干嘛。"],
            "expression": "relaxed",
        },
        "body": {
            "lines": ["……你站这么近干嘛。", "嗯？我在呢。"],
            "expression": "relaxed",
        },
    },
    "objects": {
        "book": {
            "lines": ["这本我还没看完呢。", "书签别给我弄掉了。"],
            "expression": "relaxed",
        },
        "mug": {
            "lines": ["杯子里还有水，小心一点。", "你要喝一口吗？"],
            "expression": "relaxed",
        },
        "cushion": {
            "lines": ["靠垫要放回原位啦。", "你要坐这边？"],
            "expression": "happy",
        },
        "notebook": {
            "lines": ["那本是我记东西的。", "别乱翻我的笔记。"],
            "expression": "surprised",
        },
        "plush": {
            "lines": ["鲸鱼玩偶不能抢。", "它比你软多了。"],
            "expression": "happy",
        },
    },
}

IDLE_KINDS = ("window", "read", "tail", "phone", "stretch")
EXPRESSIONS = {"happy", "shy", "relaxed", "surprised", "angry", "sad"}

PROFILE_PROMPT = """你是文字角色小世界的“动作导演”。
只根据给定人格，为实时互动写一份很短的 JSON 反应档案；不要改变人格，不要评价用户。

只输出 JSON，不要解释，不要 Markdown 代码围栏。格式必须严格是：
{
  "attention": {"curiosity": 0.0到1.0, "patience": 0.0到1.0, "initiative": 0.0到1.0},
  "idle": [
    {"kind": "window/read/tail/phone/stretch 之一", "text": "她空闲时做这件事的一句短描述"}
  ],
  "notice": {"lines": ["她注意到用户时可能说的1到2句"], "expression": "happy/shy/relaxed/surprised/angry/sad 之一"},
  "call": {"lines": ["用户叫她时她可能回应的1到2句"], "expression": "happy/shy/relaxed/surprised/angry/sad 之一"},
  "zones": {
    "head": {"lines": ["被摸头时可能说的2到3句口语短句"], "expression": "happy/shy/relaxed/surprised/angry/sad 之一"},
    "ear": {"lines": ["被碰耳朵时可能说的2到3句"], "expression": "..."},
    "tail": {"lines": ["被碰尾巴时可能说的2到3句"], "expression": "..."},
    "hand": {"lines": ["被牵手/碰手时可能说的2到3句"], "expression": "..."},
    "body": {"lines": ["被靠近/碰身体时可能说的2到3句"], "expression": "..."}
  },
  "objects": {
    "book": {"lines": ["书被碰时可能说的1到2句"], "expression": "..."},
    "mug": {"lines": ["杯子被碰时可能说的1到2句"], "expression": "..."},
    "cushion": {"lines": ["靠垫被碰时可能说的1到2句"], "expression": "..."},
    "notebook": {"lines": ["笔记本被碰时可能说的1到2句"], "expression": "..."},
    "plush": {"lines": ["鲸鱼玩偶被碰时可能说的1到2句"], "expression": "..."}
  }
}

要求：
- idle 写 4 到 5 件，至少用到 3 种 kind：
  window=在窗边看/发呆，read=看书，tail=看或玩自己的尾巴，
  phone=刷手机/看视频，stretch=伸懒腰/活动身体。
- idle 的 text 会直接显示在房间状态栏，写成一句自然的短描述。
- idle 只写当前房间能立刻表现的小动作（看窗、翻书、看尾巴、刷手机、伸懒腰）；
  不要写走开、躺到床上、窝进沙发等需要另做移动/坐卧系统的动作。
- 每句都像她当场会说的话，短、自然、有语气，不要解释设定。
- 不同区域、道具、注意到用户、被叫到时反应要不同；不要全部写成害羞或正经。
- 她可以躲开、嘴硬、开玩笑、靠近、警告或纵容；距离、尺度和用词都按人格来。
"""


def _clean_line(value: Any) -> str:
    text = str(value or "").strip().replace("\n", " ")
    return text[:120]


def _clean_expression(value: Any, default: str) -> str:
    expression = str(value or "").strip()
    return expression if expression in EXPRESSIONS else default


def _infer_idle_kind(text: str) -> str:
    if any(word in text for word in ("窗", "发呆", "外面", "雨", "云")):
        return "window"
    if any(word in text for word in ("书", "读", "翻页")):
        return "read"
    if any(word in text for word in ("尾巴", "尾鳍")):
        return "tail"
    if any(word in text for word in ("手机", "视频", "刷", "屏幕")):
        return "phone"
    if any(word in text for word in ("伸懒腰", "活动", "肩膀", "拉伸")):
        return "stretch"
    return ""


def _merge_reaction(
    base: dict[str, Any],
    incoming: Any,
) -> None:
    if not isinstance(incoming, dict):
        return
    lines = incoming.get("lines")
    if isinstance(lines, list):
        clean = [_clean_line(item) for item in lines if _clean_line(item)]
        if clean:
            base["lines"] = clean[:3]
    base["expression"] = _clean_expression(incoming.get("expression"), base["expression"])


def _merge_profile(raw: Any) -> dict[str, Any]:
    profile = json.loads(json.dumps(DEFAULT_PROFILE, ensure_ascii=False))
    if not isinstance(raw, dict):
        return profile

    attention = raw.get("attention")
    if isinstance(attention, dict):
        for key in ("curiosity", "patience", "initiative"):
            try:
                profile["attention"][key] = max(0.0, min(1.0, float(attention.get(key))))
            except (TypeError, ValueError):
                pass

    idle = raw.get("idle")
    if isinstance(idle, list):
        activities: list[dict[str, str]] = []
        used = set()
        for index, item in enumerate(idle):
            if isinstance(item, dict):
                text = _clean_line(item.get("text") or item.get("label"))
                kind = str(item.get("kind") or "").strip()
            else:
                text = _clean_line(item)
                kind = ""
            if not text or text in used:
                continue
            used.add(text)
            if kind not in IDLE_KINDS:
                kind = _infer_idle_kind(text) or IDLE_KINDS[index % len(IDLE_KINDS)]
            activities.append({"kind": kind, "text": text})
            if len(activities) >= 5:
                break
        if activities:
            for fallback in DEFAULT_PROFILE["idle"]:
                if len(activities) >= 3:
                    break
                if fallback["text"] not in used:
                    activities.append(dict(fallback))
                    used.add(fallback["text"])
            profile["idle"] = activities

    _merge_reaction(profile["notice"], raw.get("notice"))
    _merge_reaction(profile["call"], raw.get("call"))

    zones = raw.get("zones")
    if isinstance(zones, dict):
        for key, base in profile["zones"].items():
            incoming = zones.get(key)
            _merge_reaction(base, incoming)

    objects = raw.get("objects")
    if isinstance(objects, dict):
        for key, base in profile["objects"].items():
            incoming = objects.get(key)
            _merge_reaction(base, incoming)
    return profile


def parse_profile(text: str) -> dict[str, Any] | None:
    """从模型输出里取第一段完整 JSON，并合并缺省字段。"""
    match = re.search(r"\{.*\}", text or "", re.S)
    if not match:
        logger.warning("小世界反应档案没有 JSON：{}", (text or "")[:1200])
        return None
    try:
        return _merge_profile(json.loads(match.group(0)))
    except (json.JSONDecodeError, TypeError, ValueError):
        logger.warning("小世界反应档案 JSON 解析失败：{}", (text or "")[:1200])
        return None


def fallback_profile() -> dict[str, Any]:
    return json.loads(json.dumps(DEFAULT_PROFILE, ensure_ascii=False))


def prompt_messages(friend: Friend) -> list[dict[str, str]]:
    samples = ""
    try:
        samples = str(friend.voice_examples or "")[:1200]
    except Exception:  # noqa: BLE001
        samples = ""
    return [
        {"role": "system", "content": PROFILE_PROMPT},
        {
            "role": "user",
            "content": (
                f"角色名：{friend.name}\n"
                f"人格正文：\n{friend.personality or ''}\n\n"
                f"声音/说话样本（只用来抓语气，不要照抄）：\n{samples}"
            ),
        },
    ]
