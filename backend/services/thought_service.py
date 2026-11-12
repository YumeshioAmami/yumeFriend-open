"""小纸条 / 念头（D192）：**有寿命的一次性惦记**。

用户 2026-09-27 拍板的规则（进 / 说 / 销账都要能判断，不能靠形容词）：

```
进（写）  ① 她自己起意（工具 save_thought）—— 只准这四类：
             答应了的事 / 他在意的事有下文 / 她刚想说又咽回去的想法 / 有明确时间点的
          ② 同一件事已经有纸条 → **合并**（改写那张），不新增
          ③ **不设条数上限**（D243 改版第 3 条：以前 MAX_PENDING=3 太紧）
说（消费） 心跳时按 [到点且到时间] > [随缘·最旧的] 挑一张；**一张一轮**
          日常对话里给她看**最多 INJECT_LIMIT（3）条**（表无限，给她看的有限）
销（结算） 说了 → **待回**（不是删！）；他开口了 → 给一次机会后销账；
          到点型（到点后 3 天）/ 随缘型（3 天）→ 作废；待回挂满 7 天 → 放下
```

⚠️ "说了"**不等于**"销账"：他说了但不回 → 转 `awaiting`（还惦记着，但**别马上再提**，
隔一段时间 / 换个时机再说 —— D243 改版取消了"最多提 1~2 次"的硬上限，分寸写进提示词）。
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.models import Thought
from backend.services import memory_service

KINDS = ("casual", "due")  # v1 就这两档（"等某个事件"等事件触发器落地再加）
STATES = ("pending", "awaiting", "done", "expired")
# v4.0（D243 改版第 3 条）：**不设上限**，改成"每轮注入给她看的封顶"——
# 表的条数不限，**给她看的有限**（否则上下文和体感都会炸）。
INJECT_LIMIT = 3
LIFE_DAYS = {"casual": 3, "due": 3}  # 待说的寿命：随缘从"写"起算、到点型从"到点"起算
AWAITING_DAYS = 7  # 说了但他一直没回：挂这么久就自己放下
MERGE_THRESHOLD = 0.7  # 同一件事 → 合并（比记忆的去重松一点：纸条是自然语言）


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).isoformat()


def _parse(iso: str | None) -> datetime | None:
    if not iso:
        return None
    try:
        parsed = datetime.fromisoformat(str(iso).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _expiry(kind: str, due_at: str | None, moment: datetime) -> str:
    """待说的寿命：随缘型从"写"起算、到点型从"到点"起算（D192）。"""
    base = _parse(due_at) or moment
    return _iso(base + timedelta(days=LIFE_DAYS.get(kind, 3)))


def _alive(session: Session, friend_id: str) -> list[Thought]:
    rows = session.execute(
        select(Thought)
        .where(Thought.friend_id == friend_id, Thought.state.in_(("pending", "awaiting")))
        .order_by(Thought.created_at)
    ).scalars()
    return list(rows)


def alive(session: Session, friend_id: str) -> list[Thought]:
    """还活着的纸条（待说 + 待回）—— 给界面/测试看。"""
    return _alive(session, friend_id)


def add(
    session: Session,
    friend_id: str,
    *,
    content: str,
    kind: str = "casual",
    due_at: str | None = None,
    source: str = "model",
    now: datetime | None = None,
) -> tuple[Thought, bool]:
    """写一张纸条。返回 `(thought, merged)`。

    合并（同一件事）在这里兜住 —— **不靠模型自觉**。条数**不设上限**（D243 改版），
    控制体感靠"每轮注入封顶"（`notes_for_turn`）。

    `now`：可注入的"现在"（跟 `purge_expired` / `mark_said` 一致）——测试要能脱离真实日期跑。
    """
    text = (content or "").strip()
    if not text:
        raise ValueError("纸条不能是空的")
    kind = (kind or "casual").strip().lower()
    if kind not in KINDS:
        kind = "casual"
    due_at = (str(due_at).strip() or None) if due_at else None
    if kind == "due" and _parse(due_at) is None:
        raise ValueError("到点型要给一个 due_at（ISO8601，例如 2026-09-28T09:00:00+08:00）")
    moment = now or _now()

    pending = [t for t in _alive(session, friend_id) if t.state == "pending"]
    # ① 同一件事 → 合并（改写已有那张）
    for old in pending:
        if memory_service.similarity(old.content, text) >= MERGE_THRESHOLD:
            old.content = text
            old.kind = kind
            old.due_at = due_at or old.due_at
            old.expires_at = _expiry(kind, due_at or old.due_at, moment)
            old.updated_at = _iso(moment)
            session.flush()
            return old, True

    thought = Thought(
        id=str(uuid.uuid4()),
        friend_id=friend_id,
        content=text,
        kind=kind,
        due_at=due_at,
        state="pending",
        source=source,
        expires_at=_expiry(kind, due_at, moment),
        created_at=_iso(moment),
    )
    session.add(thought)
    session.flush()
    return thought, False


def add_for_schedule(
    session: Session,
    friend_id: str,
    *,
    schedule,  # backend.models.Schedule（不 import，免得循环）
    said: bool = False,
    now: datetime | None = None,
) -> Thought:
    """日程派生的纸条（v4.0 / D243）：**到日 / reminder 响完都走这里** —— 纸条是 active 的统一出口。

    · 同一条日程已有活着的纸条 → 不重复建（返回已有的）；
    · `said=True`（reminder 已经替她说了一次）→ 直接落 `awaiting`，避免"到点必响 + 立刻生成的纸条"
      连着问两遍（D243 改版专门警告的那点）。
    """
    existing = (
        session.execute(
            select(Thought).where(
                Thought.schedule_id == schedule.id,
                Thought.state.in_(("pending", "awaiting")),
            )
        )
        .scalars()
        .first()
    )
    if existing is not None:
        return existing
    moment = now or _now()
    thought = Thought(
        id=str(uuid.uuid4()),
        friend_id=friend_id,
        content=schedule.title,
        kind="casual",
        state="awaiting" if said else "pending",
        source="schedule",
        schedule_id=schedule.id,
        said_at=_iso(moment) if said else None,
        # 派生纸条的寿命跟日程的硬上限对齐（7 天）：到日那天进，留到日程归档
        expires_at=_iso(moment + timedelta(days=AWAITING_DAYS)),
        created_at=_iso(moment),
    )
    session.add(thought)
    session.flush()
    return thought


def next_thought(session: Session, friend_id: str, *, now: datetime | None = None) -> Thought | None:
    """心跳时挑一张：**[到点且到时间] > [随缘·最旧的]**（最旧的先上，免得躺到过期）。"""
    moment = now or _now()
    pending = [t for t in _alive(session, friend_id) if t.state == "pending"]
    ready = [
        t for t in pending if t.kind == "due" and (_parse(t.due_at) or moment) <= moment
    ]
    if ready:
        return min(ready, key=lambda t: t.due_at or "")
    casual = [t for t in pending if t.kind == "casual"]
    return min(casual, key=lambda t: t.created_at) if casual else None


def mark_said(session: Session, thought: Thought, *, now: datetime | None = None) -> None:
    """说了 → 转 **待回**（不是销账）—— 他没回的话，这事还惦记着，但**不许再主动提**。"""
    moment = now or _now()
    thought.state = "awaiting"
    thought.said_at = _iso(moment)
    thought.expires_at = _iso(moment + timedelta(days=AWAITING_DAYS))
    thought.updated_at = _iso(moment)
    session.flush()


# 他开口时给她看纸条时的分寸（D243 改版：**不设硬上限**，分寸写进提示词）
NOTES_TURN_RULES = (
    "（这些是你心里惦记着的事。**不一定非要说** —— 时机合适就顺口提一句；"
    "**先看你们刚聊的**：他已经提过某件事办完了、或你俩已经有结论的，别提（那会像失忆）；"
    "刚提过、或最近没进展的，这次就先别提；隔一段时间、换个时机或换个说法再说。"
    "别像念待办，也别一次把几条全倒出来。）"
)


def notes_for_turn(
    session: Session, friend_id: str, *, limit: int = INJECT_LIMIT, now: datetime | None = None
) -> str | None:
    """他开口时交给她"你惦记的事" —— **最多 `limit` 条**（表无限，给她看的有限，D243 改版）。

    待回（说了他没回）排在前面，其次最旧的。**不改纸条状态**：她这次提不提由她自己定，
    分寸在 `NOTES_TURN_RULES` 里写死（旧版是"给一次就销账"，D243 改版改成无限制 + 提示词收敛）。
    """
    del now  # 保留参数（跟其它函数一致，测试可注入）；排序不依赖"现在"
    rows = _alive(session, friend_id)
    if not rows:
        return None
    rows.sort(key=lambda t: (0 if t.state == "awaiting" else 1, t.created_at))
    lines = [f"- {t.content.strip()}" for t in rows[:limit]]
    return "\n".join(lines) + "\n" + NOTES_TURN_RULES


def thought_hint(thought: Thought) -> str:
    """心跳里交给她"你攒着想说的那件事"（优先级高于随口冒泡）。"""
    return (
        f"[系统提示] 你心里惦记着要跟他说一件事：{thought.content}\n"
        "**先看你们最近的对话**：如果他刚提过这事已经办完了、或你们已经聊过并有结论了，"
        "就别再提（那会像失忆）—— 这次只回 `[沉默]` 就行。\n"
        "否则就用你自己的话说出来（一句话就够，别像在念待办）。\n"
        "如果他这次还是没回，就先别追 —— 等他下次开口时再顺口提。"
    )


def purge_expired(session: Session, *, now: datetime | None = None) -> int:
    """到期清理：待说 / 待回 过了 `expires_at` → 作废（**自然放下**，不留成债）。"""
    moment = now or _now()
    count = 0
    for thought in session.execute(
        select(Thought).where(Thought.state.in_(("pending", "awaiting")))
    ).scalars():
        at = _parse(thought.expires_at)
        if at is None:
            continue
        if at <= moment:
            thought.state = "expired"
            thought.updated_at = _iso(moment)
            count += 1
    if count:
        session.flush()
    return count


def _to_dict(thought: Thought) -> dict:
    return {
        "id": thought.id,
        "content": thought.content,
        "kind": thought.kind,
        "state": thought.state,
        "due_at": thought.due_at,
        "said_at": thought.said_at,
        "expires_at": thought.expires_at,
        "created_at": thought.created_at,
    }


def list_for_ui(session: Session, friend_id: str) -> dict:
    """给界面看的：**还惦记着的**（待说 + 待回），外加各状态条数。"""
    rows = _alive(session, friend_id)
    pending = [t for t in rows if t.state == "pending"]
    awaiting = [t for t in rows if t.state == "awaiting"]
    return {
        "thoughts": [_to_dict(t) for t in sorted(rows, key=lambda t: t.created_at)],
        "counts": {"pending": len(pending), "awaiting": len(awaiting), "alive": len(rows)},
        "max_pending": None,  # v4.0（D243 改版）：不设上限；每轮注入封顶见 INJECT_LIMIT
    }


def drop(session: Session, thought_id: str, *, now: datetime | None = None) -> None:
    """用户划掉 / 她自己改主意 → 作废（找不到就是已经被清掉了，不报错）。"""
    thought = session.get(Thought, thought_id)
    if thought is None:
        return
    moment = now or _now()
    thought.state = "expired"
    thought.updated_at = _iso(moment)
    session.flush()
