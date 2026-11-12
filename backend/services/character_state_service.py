"""角色运行时状态（V6.0-1 / D296）。

这里只做三件事：
  1. 读取 / 创建每个朋友的状态行；
  2. 把模型产出的 state_delta 应用到状态；
  3. 把状态渲染成自然语言块（不暴露数据库字段名）。

它不是“情绪模板”。系统只提供通用评价骨架，具体敏感点归 personality。
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from typing import Any, Mapping

from loguru import logger
from sqlalchemy.orm import Session

from backend.db import session_scope
from backend.models import Friend, FriendState


DEFAULT_AFFECT: dict[str, float] = {
    "valence": 0.0,
    "arousal": 0.45,
    "security": 0.55,
    "connection": 0.55,
}
DEFAULT_ENERGY = 0.55
MAX_NOTE_CHARS = 600
MAX_CONCERNS = 5
MAX_OPEN_LOOPS = 8
MAX_THREAD_ITEMS = 8


STATE_RUNTIME_RULES = """【每轮开口前：先更新你自己的状态，再说话】
你不是先判断“该怎么回”。先感受这件事对你意味着什么：
它碰到你在乎的东西了吗？是冲你来的吗？让关系更近还是更远？
你现在的情绪和想做的事，和上一刻相比变了吗？

在正文最前面输出一行（用户看不到，不要解释它，也不要在正文里提到它）：
<state_delta>{"appraisal":"一句话说清为什么变","affect_delta":{"valence":0.0,"arousal":0.0,"security":0.0,"connection":0.0},"energy":0.55,"intent":"靠近/逗/怼/哄/观察/沉默/解释/问清楚/躲开","inner_note":"你私下的第一人称短句","relationship_note":"你此刻对你们关系的感受","concerns":["心里挂着的事"],"open_loops_add":["还没说完/没解决的事"],"open_loops_resolve":["已经放下的事"],"active_thread":{"goal":"正在一起想什么","constraints":["约束"],"candidates":[{"idea":"方向","note":"备注"}],"decisions":["已经定了什么"],"open_questions":["还没定什么"]}}</state_delta>
然后直接写你要说的话。不要用 <tool> 伪装这个块。

规则：
· affect_delta 是变化量，不是绝对值；没有明显变化就全写 0。
· energy 是 0~1 的当前精力；intent 用短词，不要写成长句。
· inner_note 只写你此刻真实的短想法，不要编剧情、不要写给用户看。
  如果此刻有想继续、想问、想做的事，就自然写进去；没有也不必硬造。
  正文说多少、有没有下文，跟着这个真实想法走，不按检查表凑。
· 不要把 inner_note / 自我分析写进可见正文；情绪让它从语气、停顿、动作和短句里出来。
· 只有真有未完成的事才写 open_loops_add；放下了才写 open_loops_resolve。
  有 open_loops 时，正文可以自然提一句，但不要像任务清单。
· 只有当 TA 明确要你一起想/规划/比较时，才维护 active_thread；普通闲聊不要建。
  一起想完了、换话题了，就把 active_thread 设为 null。
· 这个块是给你自己用的，不是回复内容。"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _loads(raw: str | None, default: Any) -> Any:
    try:
        value = json.loads(raw or "")
    except (TypeError, ValueError):
        return default
    return value if isinstance(value, type(default)) else default


def _dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False)


def _clamp(value: Any, low: float, high: float, default: float) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    if number != number:  # NaN
        return default
    return max(low, min(high, number))


def _clean_text(value: Any, *, limit: int = MAX_NOTE_CHARS) -> str:
    if value is None:
        return ""
    return re.sub(r"\s+", " ", str(value)).strip()[:limit]


def _clean_list(value: Any, *, limit: int) -> list[str]:
    if not isinstance(value, (list, tuple)):
        return []
    out: list[str] = []
    seen: set[str] = set()
    for item in value:
        text = _clean_text(item, limit=160)
        key = text.lower()
        if text and key not in seen:
            seen.add(key)
            out.append(text)
        if len(out) >= limit:
            break
    return out


def get_or_create(session: Session, friend_id: str) -> FriendState:
    state = session.get(FriendState, friend_id)
    if state is not None:
        return state
    # 先确认朋友还在；不然只会在 flush 时撞 FK，错误更难读。
    if session.get(Friend, friend_id) is None:
        raise ValueError(f"friend not found: {friend_id}")
    state = FriendState(
        friend_id=friend_id,
        affect=_dumps(DEFAULT_AFFECT),
        energy=DEFAULT_ENERGY,
        concerns="[]",
        intent="observe",
        inner_note="",
        relationship_note="",
        open_loops="[]",
        active_thread=None,
        updated_at=_now(),
    )
    session.add(state)
    session.flush()
    return state


def state_to_dict(state: FriendState) -> dict[str, Any]:
    """调试 / 测试用的状态快照（不用于 prompt）。"""
    affect = _loads(state.affect, DEFAULT_AFFECT)
    return {
        "friend_id": state.friend_id,
        "affect": {key: float(affect.get(key, DEFAULT_AFFECT[key])) for key in DEFAULT_AFFECT},
        "energy": float(state.energy),
        "concerns": _loads(state.concerns, []),
        "intent": state.intent,
        "inner_note": state.inner_note,
        "relationship_note": state.relationship_note,
        "open_loops": _loads(state.open_loops, []),
        "active_thread": _loads(state.active_thread, {}) if state.active_thread else None,
        "updated_at": state.updated_at,
    }


def _affect_words(affect: Mapping[str, float]) -> list[str]:
    words: list[str] = []
    valence = float(affect.get("valence", 0.0))
    arousal = float(affect.get("arousal", 0.45))
    security = float(affect.get("security", 0.55))
    connection = float(affect.get("connection", 0.55))

    if valence >= 0.3:
        words.append("心情还不错")
    elif valence <= -0.3:
        words.append("心里有点发沉")
    else:
        words.append("心情平着")

    if arousal >= 0.7:
        words.append("有点绷着/有劲")
    elif arousal <= 0.25:
        words.append("懒懒的")

    if security <= 0.3:
        words.append("不太踏实")
    elif security >= 0.7:
        words.append("比较安心")

    if connection >= 0.7:
        words.append("想靠近")
    elif connection <= 0.3:
        words.append("有点想退开")

    return words


def _energy_words(energy: float) -> str:
    if energy >= 0.72:
        return "有精神"
    if energy <= 0.3:
        return "累，想少说两句"
    return "还行"


def to_prompt(state: FriendState | None) -> str:
    """渲染成模型能直接感受的自然语言，不暴露字段名和分数。"""
    if state is None:
        return ""
    affect = _loads(state.affect, DEFAULT_AFFECT)
    concerns = _loads(state.concerns, [])
    loops = _loads(state.open_loops, [])

    lines = ["# 你现在", "你现在的状态：" + "，".join(_affect_words(affect))]
    lines.append(f"精力：{_energy_words(float(state.energy))}")
    if concerns:
        lines.append("你心里挂着：" + "；".join(str(item) for item in concerns[:MAX_CONCERNS]))
    if state.intent:
        lines.append(f"你现在更想：{state.intent}")
    if state.relationship_note:
        lines.append(f"你对你们之间：{state.relationship_note}")
    if state.inner_note:
        lines.append(f"你私下里想：{state.inner_note}")
    if loops:
        texts = []
        for item in loops[:MAX_OPEN_LOOPS]:
            if isinstance(item, Mapping):
                text = str(item.get("text") or "").strip()
                if text:
                    texts.append(text)
            else:
                text = str(item).strip()
                if text:
                    texts.append(text)
        if texts:
            lines.append("你还惦记着：" + "；".join(texts))
    thread = _loads(state.active_thread, {}) if state.active_thread else None
    if isinstance(thread, Mapping) and thread.get("goal"):
        lines.extend(["", "# 你们正在一起想", f"目标：{thread.get('goal')}"])
        constraints = thread.get("constraints")
        if isinstance(constraints, list) and constraints:
            lines.append("约束：" + "；".join(str(item) for item in constraints[:MAX_THREAD_ITEMS]))
        candidates = thread.get("candidates")
        if isinstance(candidates, list) and candidates:
            ideas: list[str] = []
            for item in candidates[:MAX_THREAD_ITEMS]:
                if isinstance(item, Mapping):
                    idea = str(item.get("idea") or "").strip()
                    note = str(item.get("note") or "").strip()
                    if idea:
                        ideas.append(idea + (f"（{note}）" if note else ""))
                else:
                    idea = str(item).strip()
                    if idea:
                        ideas.append(idea)
            if ideas:
                lines.append("目前的方向：" + "；".join(ideas))
        decisions = thread.get("decisions")
        if isinstance(decisions, list) and decisions:
            lines.append("已经定了：" + "；".join(str(item) for item in decisions[:MAX_THREAD_ITEMS]))
        questions = thread.get("open_questions")
        if isinstance(questions, list) and questions:
            lines.append("还没定：" + "；".join(str(item) for item in questions[:MAX_THREAD_ITEMS]))
        lines.append("（接着这个工作区想，不要重新开始；一起想完了就把它放下。）")
    lines.append("（这不是命令，是你此刻的状态。你从这里面说话。）")
    return "\n".join(lines)


def apply_delta(session: Session, friend_id: str, delta: Mapping[str, Any] | None) -> FriendState:
    """应用模型产出的状态变化。坏字段忽略，不让它拖垮这一轮。"""
    state = get_or_create(session, friend_id)
    if not isinstance(delta, Mapping):
        return state

    current_affect = _loads(state.affect, DEFAULT_AFFECT)
    affect_delta = delta.get("affect_delta")
    if not isinstance(affect_delta, Mapping):
        affect_delta = delta.get("affect") if isinstance(delta.get("affect"), Mapping) else {}
    next_affect: dict[str, float] = {}
    for key, default in DEFAULT_AFFECT.items():
        base = _clamp(current_affect.get(key, default), -1.0, 1.0, default)
        change = affect_delta.get(key, 0)
        next_affect[key] = round(_clamp(base + _clamp(change, -1.0, 1.0, 0.0), -1.0, 1.0, base), 3)
    state.affect = _dumps(next_affect)

    if "energy" in delta:
        state.energy = _clamp(delta.get("energy"), 0.0, 1.0, float(state.energy))

    if isinstance(delta.get("concerns"), (list, tuple)):
        state.concerns = _dumps(_clean_list(delta.get("concerns"), limit=MAX_CONCERNS))
    elif isinstance(delta.get("concerns_add"), (list, tuple)):
        merged = _clean_list(_loads(state.concerns, []) + list(delta.get("concerns_add")), limit=MAX_CONCERNS)
        state.concerns = _dumps(merged)

    if isinstance(delta.get("intent"), str):
        state.intent = _clean_text(delta.get("intent"), limit=40) or state.intent
    if isinstance(delta.get("inner_note"), str):
        state.inner_note = _clean_text(delta.get("inner_note"))
    if isinstance(delta.get("relationship_note"), str):
        state.relationship_note = _clean_text(delta.get("relationship_note"))
    if "active_thread" in delta:
        state.active_thread = _normalize_thread(delta.get("active_thread"))

    loops = _loads(state.open_loops, [])
    normalized: list[dict[str, Any]] = []
    for item in loops if isinstance(loops, list) else []:
        if isinstance(item, Mapping):
            text = _clean_text(item.get("text"), limit=160)
            if text:
                normalized.append({"text": text, "since": item.get("since") or state.updated_at})
        else:
            text = _clean_text(item, limit=160)
            if text:
                normalized.append({"text": text, "since": state.updated_at})

    for item in delta.get("open_loops_resolve") or []:
        needle = _clean_text(item, limit=160).lower()
        if needle:
            normalized = [
                loop for loop in normalized if needle not in str(loop.get("text", "")).lower()
            ]

    for item in delta.get("open_loops_add") or []:
        text = _clean_text(item, limit=160)
        if text and not any(text.lower() == str(loop.get("text", "")).lower() for loop in normalized):
            normalized.append({"text": text, "since": _now()})

    state.open_loops = _dumps(normalized[:MAX_OPEN_LOOPS])
    state.updated_at = _now()
    session.flush()
    return state


def _normalize_thread(value: Any) -> str | None:
    if not isinstance(value, Mapping):
        return None
    goal = _clean_text(value.get("goal"), limit=200)
    if not goal:
        return None

    def items(key: str) -> list[str]:
        return _clean_list(value.get(key), limit=MAX_THREAD_ITEMS)

    candidates: list[dict[str, str]] = []
    raw_candidates = value.get("candidates")
    if isinstance(raw_candidates, list):
        for item in raw_candidates[:MAX_THREAD_ITEMS]:
            if isinstance(item, Mapping):
                idea = _clean_text(item.get("idea") or item.get("text"), limit=200)
                note = _clean_text(item.get("note"), limit=200)
            else:
                idea = _clean_text(item, limit=200)
                note = ""
            if idea:
                candidates.append({"idea": idea, "note": note})
    return _dumps(
        {
            "goal": goal,
            "constraints": items("constraints"),
            "candidates": candidates,
            "decisions": items("decisions"),
            "open_questions": items("open_questions"),
            "updated_at": _now(),
        }
    )


def apply_delta_by_id(friend_id: str, delta: Mapping[str, Any] | None) -> None:
    """给流式链路用的独立会话入口（放到线程里跑，别堵事件循环）。"""
    try:
        with session_scope() as session:
            apply_delta(session, friend_id, delta)
    except Exception as exc:  # noqa: BLE001 —— 状态写失败不许拖垮回复
        logger.warning("角色状态更新失败（不影响这轮回复）：{}", exc)
