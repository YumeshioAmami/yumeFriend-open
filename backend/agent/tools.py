"""工具执行层（LLM协议§4 / architecture.md §5.1）。

模型吐出来的工具调用，以 `Actor.AGENT` 的身份交给 service 层执行。
权限判断在 service 层（宪法附录 B），这一层只负责：
参数兜底、异常兜底、计时、把结果归一成 `tool_result`。

system prompt 里的工具清单由这里的 manifest 生成（`prompt_lines()`），
所以「说了哪些工具」和「真能执行哪些工具」不会漂移 —— 加工具只改这一处。
"""

from __future__ import annotations

import asyncio
import json
import re
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Collection

from loguru import logger
from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.features import catalog as feature_catalog
from backend.features import flags as feature_flags
from backend.models import Message
from backend.services import (
    approval_service,
    embedding_service,
    file_service,
    friend_service,
    image_service,
    knowledge_service,
    memory_service,
    schedule_service,
    settings_service,
    style_profile_service,
    web_search_service,
)
from backend.services.actor import Actor
from backend.services.errors import ServiceError
from backend.timefmt import local_stamp

# 模型经常把 importance 写成 high/medium/low（实测过），这里兜住
_IMPORTANCE_WORDS = {
    "high": 0.9,
    "important": 0.9,
    "medium": 0.5,
    "normal": 0.5,
    "low": 0.3,
}


def _importance(value: Any, default: float = 0.5) -> float:
    if value is None:
        return default
    if isinstance(value, (int, float)):
        return max(0.0, min(1.0, float(value)))
    text = str(value).strip().lower()
    if text in _IMPORTANCE_WORDS:
        return _IMPORTANCE_WORDS[text]
    try:
        return max(0.0, min(1.0, float(text)))
    except ValueError:
        logger.warning("importance 不认识，按默认值处理：{!r}", value)
        return default


def _text(value: Any, default: str = "") -> str:
    if value is None:
        return default
    return str(value).strip()


def _normalize_tag(value: Any) -> str:
    text = _text(value).lower()
    if text in memory_service.VALID_TAGS:
        return text
    if text:
        logger.warning("模型给了非法 tag {!r}，按 normal 处理", value)
    return "normal"


def _normalize_layer(value: Any) -> str:
    text = _text(value).lower()
    if text in memory_service.VALID_LAYERS:
        return text
    if text:
        logger.warning("模型给了非法 layer {!r}，按 long_term 处理", value)
    return "long_term"


# v4.0（D243）：日程的时间是**本地**的（`date`=YYYY-MM-DD / `time`=HH:MM）。
# 宽容收：date 可以是 YYYY-MM-DD、ISO8601、或相对写法（10m / 2h / 1d）；
# time 可以是 15:30 / 9:5 / 15:00:00。为什么要宽容：让模型自己算准时间，错时间 = 没提醒。
_RELATIVE = re.compile(r"^\+?(\d+)(秒|s|分钟|分|m|小时|时|h|天|d)$", re.IGNORECASE)
_UNIT_SECONDS = {
    "秒": 1, "s": 1,
    "分钟": 60, "分": 60, "m": 60,
    "小时": 3600, "时": 3600, "h": 3600,
    "天": 86400, "d": 86400,
}


def parse_schedule_when(args: dict[str, Any]) -> tuple[str, str | None]:
    """从工具参数凑出 `(本地 date, 本地 time)`。"""
    raw = args.get("date") or args.get("day") or args.get("when") or args.get("trigger_time")
    if raw in (None, ""):
        raise ValueError("日程要有个日期（date，例如 2026-10-09）")
    text = _text(raw)
    match = _RELATIVE.match(text.replace(" ", ""))
    if match:
        amount = int(match.group(1))
        unit = match.group(2).lower()
        moment = datetime.now().astimezone() + timedelta(seconds=amount * _UNIT_SECONDS[unit])
        return moment.strftime("%Y-%m-%d"), moment.strftime("%H:%M")
    if "T" in text or " " in text:
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            pass
        else:
            local = parsed if parsed.tzinfo else parsed.astimezone()
            local = local.astimezone()
            return local.strftime("%Y-%m-%d"), local.strftime("%H:%M")
    return text, args.get("time") or args.get("at")


# ---------- 各个工具 ----------


def _save_memory(session: Session, friend_id: str, args: dict[str, Any]) -> dict[str, Any]:
    memory, merged = memory_service.save(
        session,
        Actor.AGENT,
        friend_id,
        content=_text(args.get("content")),
        tag=_normalize_tag(args.get("tag")),
        layer=_normalize_layer(args.get("layer")),
        context=_text(args.get("context")) or None,
        importance=_importance(args.get("importance")),
    )
    return {
        "id": memory.id,
        "merged": merged,  # True = 命中已有记忆、刷新了它（D28）
        "layer": memory.layer,
        "tag": memory.tag,
    }


def _update_memory(session: Session, friend_id: str, args: dict[str, Any]) -> dict[str, Any]:
    memory_id = _text(args.get("id"))
    if not memory_id:
        raise ValueError("update_memory 需要 id（可以先用 recall_memory 拿到）")
    content = args.get("content")
    importance = args.get("importance")
    layer = args.get("layer")
    if content is None and importance is None and layer is None:
        raise ValueError("update_memory 至少要给 content / importance / layer 之一")
    if layer is not None:
        layer = _text(layer).strip()
        # 这里**故意不兜底成 long_term**：改层是明确动作，给错层必须报错让她重来，
        # 否则会静默把用户的记忆换到别的层（D230）。
        if layer not in memory_service.VALID_LAYERS:
            raise ValueError(f"layer 只能是 {memory_service.VALID_LAYERS}，收到 {layer!r}")
    memory = memory_service.update(
        session,
        Actor.AGENT,
        memory_id,
        content=_text(content) if content is not None else None,
        importance=_importance(importance) if importance is not None else None,
        layer=layer,
    )
    return {
        "id": memory.id,
        "content": memory.content,
        "importance": memory.importance,
        "layer": memory.layer,
    }


def _delete_memory(session: Session, friend_id: str, args: dict[str, Any]) -> dict[str, Any]:
    memory_id = _text(args.get("id"))
    if not memory_id:
        raise ValueError("delete_memory 需要 id（可以先用 recall_memory 拿到）")
    memory_service.delete(session, Actor.AGENT, memory_id)
    return {"id": memory_id, "deleted": True}


def _get_time(session: Session, friend_id: str, args: dict[str, Any]) -> dict[str, Any]:
    """现在几点（v3.8 / D170）。

    为什么要它：system prompt 的 `# 现在` 里**本来就有**当前时间，但模型经常不当回事
    （用户实测："我问汐音几点了他都不知道"）。给她一个能**明确查一次**的入口，
    长对话 / 工具循环里就可靠得多 —— 时间也常常是"过了多久""该不该催他睡觉"这类判断的输入。
    """
    del session, friend_id, args
    local = datetime.now()
    return {
        "今日": f"{local:%Y-%m-%d}",
        "现在": f"{local:%H:%M}",
        "星期": "周" + "一二三四五六日"[local.weekday()],
        "UTC": f"{datetime.now(timezone.utc):%Y-%m-%d %H:%M}",
        "hint": "这就是此刻的真实时间，直接用它回答/判断就行（别把这一行念给 TA）。",
    }


def _recall_memory(session: Session, friend_id: str, args: dict[str, Any]) -> dict[str, Any]:
    query = _text(args.get("query")) or _text(args.get("content"))
    if not query:
        raise ValueError("recall_memory 需要 query")
    # v3.46（D231）：**关键词 ∪ 向量**混合召回；向量不可用会自动退回纯关键词
    found = embedding_service.hybrid_recall(session, friend_id, query)
    return {
        "query": query,
        "count": len(found),
        # 带上 id，模型才可能接着调 update_memory / delete_memory
        "memories": [
            {
                "id": m.id,
                "content": m.content,
                "layer": m.layer,
                "tag": m.tag,
                "context": m.context,
            }
            for m in found
        ],
    }


# ---------- M4：入戏 / 换角色 / 调演法 ----------

# 模型有时会说中文（实测它对"模式"的理解就是中文词），顺手兜住
_MODE_ALIASES = {
    "普通": "normal",
    "正常": "normal",
    "normal": "normal",
    "入戏": "roleplay",
    "扮演": "roleplay",
    "角色": "roleplay",
    "roleplay": "roleplay",
    "rp": "roleplay",
    "工具": "tool",
    "tool": "tool",
}


def _switch_mode(session: Session, friend_id: str, args: dict[str, Any]) -> dict[str, Any]:
    """自主入戏 / 出戏（D2：入戏不用问；出戏仍只认用户的明确信号）。"""
    raw = _text(args.get("mode")) or _text(args.get("role"))
    mode = _MODE_ALIASES.get(raw.lower(), raw.lower())
    if mode not in ("normal", "roleplay", "tool"):
        raise ValueError(f"不认识的模式：{raw!r}（只能是 normal / roleplay / tool）")
    friend = friend_service.set_mode(session, Actor.AGENT, friend_id, mode)
    return {"mode": friend.current_mode, "role": friend.current_role}


def _switch_role(session: Session, friend_id: str, args: dict[str, Any]) -> dict[str, Any]:
    """换角色（隐含"要演"→ 顺带切到 roleplay，不然角色名没地方生效）。"""
    role = _text(args.get("role")) or _text(args.get("name"))
    if not role:
        raise ValueError("switch_role 需要 role（要演谁）")
    friend = friend_service.set_mode(session, Actor.AGENT, friend_id, "roleplay", role=role)
    return {"mode": friend.current_mode, "role": friend.current_role}


def _adjust_rp_playstyle(session: Session, friend_id: str, args: dict[str, Any]) -> dict[str, Any]:
    """调演法：写 `rp_play_style` 记忆，**同一个角色覆盖更新**（M4）。"""
    text = _text(args.get("text")) or _text(args.get("play_style")) or _text(args.get("content"))
    if not text:
        raise ValueError("adjust_rp_playstyle 需要 text（新的演法）")
    friend = friend_service.get(session, Actor.AGENT, friend_id)
    if not friend.current_role:
        raise ValueError("现在没在扮演谁，先 switch_role 再调演法")
    memory, updated = memory_service.save_play_style(
        session, Actor.AGENT, friend_id, role=friend.current_role, text=text
    )
    return {"role": friend.current_role, "updated": updated, "play_style_id": memory.id}


# ---------- M5 / v4.0：日程（"她和你的约定"，D243）----------


def _add_schedule(session: Session, friend_id: str, args: dict[str, Any]) -> dict[str, Any]:
    title = _text(args.get("title")) or _text(args.get("content")) or _text(args.get("text"))
    if not title:
        raise ValueError("add_schedule 需要 title（这件事是什么）")
    day, clock = parse_schedule_when(args)
    schedule = schedule_service.create(
        session,
        Actor.AGENT,
        friend_id,
        title=title,
        date=day,
        time=clock,
        nature=args.get("nature") or "remind",
        advance_minutes=args.get("advance_minutes") or 0,
        owner=args.get("owner") or "user",
        source="chat",
    )
    return {
        "id": schedule.id,
        "title": schedule.title,
        "date": schedule.date,
        "time": schedule.time,
        "nature": schedule.nature,
        "owner": schedule.owner,
        "state": schedule.state,
    }


def _find_schedule(session: Session, friend_id: str, args: dict[str, Any]):  # noqa: ANN202
    """按 id 找；没给 id 就按标题找（模型平时拿不到 id，得让它能用）。"""
    schedule_id = _text(args.get("id"))
    if schedule_id:
        schedule = session.get(schedule_service.Schedule, schedule_id)
        if (
            schedule is None
            or schedule.deleted_at is not None
            or schedule.friend_id != friend_id
        ):
            raise ValueError(f"找不到这条日程：{schedule_id}")
        return schedule

    query = (
        _text(args.get("title"))
        or _text(args.get("content"))
        or _text(args.get("query"))
        or _text(args.get("text"))
    )
    if not query:
        raise ValueError("要改/要删的日程，得给 id 或者它的大致说法")
    rows = schedule_service.list_for(session, friend_id=friend_id, include_archived=True)
    hits = [r for r in rows if query in r.title or r.title in query]
    if not hits:
        raise ValueError(f"没找到跟「{query}」对得上的日程")
    if len(hits) > 1:
        raise ValueError(f"有 {len(hits)} 条都像，说不清是哪条，请说得更具体一点")
    return hits[0]


def _list_schedules(session: Session, friend_id: str, args: dict[str, Any]) -> dict[str, Any]:
    query = _text(args.get("query")) or _text(args.get("title"))
    owner = _text(args.get("owner")) or None
    include_settled = bool(args.get("include_settled"))
    rows = schedule_service.list_for(session, friend_id=friend_id, owner=owner)
    if not include_settled:
        rows = [r for r in rows if r.state in ("pending", "active")]
    if query:
        rows = [r for r in rows if query in r.title]
    return {
        "count": len(rows),
        "schedules": [
            {
                "id": r.id,
                "title": r.title,
                "date": r.date,
                "time": r.time,
                "nature": r.nature,
                "state": r.state,
                "owner": r.owner,
                "谁记的": "他" if r.owner == "user" else "我",
            }
            for r in rows
        ],
    }


def _update_schedule(session: Session, friend_id: str, args: dict[str, Any]) -> dict[str, Any]:
    schedule = _find_schedule(session, friend_id, args)
    kwargs: dict[str, Any] = {}
    # ⚠️ v4.1（D246）：`title` 是**用来找那条的**，不是要改的字段 ——
    # 她实测会把 `update_schedule(title="喝水", time=…)` 里的 title 当"新标题"，
    # 结果把「提醒梦汐喝水」改成了「喝水」。改名走 `new_title`。
    new_title = _text(args.get("new_title"))
    if new_title:
        kwargs["title"] = new_title
    for key in ("nature", "date", "time", "advance_minutes", "owner"):
        if args.get(key) is not None:
            kwargs[key] = args[key]
    state = _text(args.get("state"))
    if state:
        alias = {
            "done": "settled",
            "完成": "settled",
            "做了": "settled",
            "settled": "settled",
            "dropped": "settled",
            "不做了": "settled",
            "取消": "settled",
            "reopen": "pending",
            "pending": "pending",
            "重开": "pending",
        }
        target = alias.get(state.lower(), state.lower())
        if target not in ("pending", "settled"):
            raise ValueError(
                "state 只能写 settled（做了/不做了）或 pending（重开/改期）；"
                "active / archived 归系统，不用你写"
            )
        kwargs["state"] = target
        if target == "settled":
            reason = _text(args.get("settled_reason") or args.get("reason"))
            kwargs["settled_reason"] = reason if reason in ("done", "dropped") else (
                "done" if state.lower() not in ("dropped", "不做了", "取消") else "dropped"
            )
    if not kwargs:
        raise ValueError("update_schedule 至少要给点要改的（date/time/state/new_title…）")
    updated = schedule_service.update(session, Actor.AGENT, schedule.id, **kwargs)
    return schedule_service.to_dict(updated)


def _remove_schedule(session: Session, friend_id: str, args: dict[str, Any]) -> dict[str, Any]:
    schedule = _find_schedule(session, friend_id, args)
    title = schedule.title
    schedule_service.remove(session, Actor.AGENT, schedule.id)
    return {"id": schedule.id, "removed": True, "title": title}


def _look_image(session: Session, friend_id: str, args: dict[str, Any]) -> dict[str, Any]:
    """把某张**旧图**重新拉进本轮上下文（D82 第三层：按需付费，只有这一刻花图片钱）。

    怎么找到"就是那张"（P2 / P3 / P4）：`no` 可以是
      · 编号（占位 `[图片#12 · 09-23 21:30 · 一只猫]` 里的数字）
      · `latest` / 不填 → **最近一张**
      · 一个**关键词**（`query`，或干脆把词写在 no 里）→ 在图片索引里找，比如"猫"
    """
    raw = args.get("no", args.get("image_no", args.get("id")))
    query = _text(args.get("query") or args.get("keyword") or args.get("内容"))
    message = None
    by_number: int | None = None
    latest_words = {"", "latest", "last", "最近", "最近一张", "最新", "最后一张", "刚才那张", "刚才"}
    if isinstance(raw, str):
        raw = raw.strip()
    if not raw and query:
        # 只给了关键词（P4：按内容找图）—— 别把它当成"最近一张"
        pass
    elif raw is None or raw in latest_words:
        message = session.execute(
            select(Message)
            .where(Message.friend_id == friend_id, Message.image_no.is_not(None))
            .order_by(Message.image_no.desc())
            .limit(1)
        ).scalar_one_or_none()
        if message is None:
            raise ValueError("你们之间还没有图")
    else:
        try:
            by_number = int(raw)
        except (TypeError, ValueError):
            # 不是编号也没说 latest → 当成关键词（P4：按内容找图）
            query = query or str(raw or "").strip()
        else:
            message = session.execute(
                select(Message).where(Message.friend_id == friend_id, Message.image_no == by_number)
            ).scalar_one_or_none()
    if message is None and query:
        # 在"图片索引"里找（大小写不敏感；没有索引的图找不到，那就如实说）
        rows = (
            session.execute(
                select(Message)
                .where(
                    Message.friend_id == friend_id,
                    Message.image_no.is_not(None),
                    Message.image_note.is_not(None),
                    Message.image_note.ilike(f"%{query}%"),
                )
                .order_by(Message.image_no.desc())
                .limit(3)
            )
            .scalars()
            .all()
        )
        if not rows and not query:
            raise ValueError("look_image 要一个图片编号（或 latest），或者一个关键词")
        if not rows:
            raise ValueError(f"没有一张图跟「{query}」对得上（我记住的图片内容里没有这个）")
        message = rows[0]
    if message is None:
        raise ValueError(f"没有第 {raw} 张图（编号可能记错了；也可以给 latest 或关键词）")
    number = int(message.image_no or 0)
    url = image_service.data_url(message.image_path)
    if url is None:
        raise ValueError(f"第 {number} 张图的文件不在了")
    # 下划线开头 = 内部字段：loop 会把它摘出来塞进多模态消息，
    # **不进 DB、不进日志、不进 tool_result 事件**（那是 1MB 级的字符串）
    return {
        "no": number,
        "时间": local_stamp(message.created_at),
        "内容": (message.image_note or None),
        "_image_data_url": url,
        "note": "这张图已经重新附在下面了",
    }


def _list_images(session: Session, friend_id: str, args: dict[str, Any]) -> dict[str, Any]:
    """翻相册（P3 / D176）：最近发过的图 —— 编号、本地时间、谁发的、（有的话）一句内容。

    为什么要有它：占位符一旦滑出历史窗口，她就**连"有过这张图"都不知道**了；
    有了这个工具，她可以"先列一遍 → 再 look_image(no)"，用户说"我说的那张图"就找得到。
    """
    raw = args.get("limit", args.get("n", args.get("count", 10)))
    try:
        limit = max(1, min(30, int(raw)))
    except (TypeError, ValueError):
        limit = 10
    rows = (
        session.execute(
            select(Message)
            .where(Message.friend_id == friend_id, Message.image_no.is_not(None))
            .order_by(Message.image_no.desc())
            .limit(limit)
        )
        .scalars()
        .all()
    )
    if not rows:
        return {"count": 0, "hint": "你们之间还没有图（他第一次发图之后这里就会有）"}
    return {
        "count": len(rows),
        "images": [
            {
                "no": int(m.image_no or 0),
                "时间": local_stamp(m.created_at),
                "谁发的": "他" if m.role == "user" else "我",
                "内容": (m.image_note or None) or "（这张我还没看过内容）",
            }
            for m in rows
        ],
        "hint": "要看哪张就把它的 no 交给 look_image（例如 look_image(no=12)）；给 look_image 一个词也行（比如「猫」）。",
    }


def _chat_proxy(session: Session) -> str:
    """搜网跟聊天走同一套"网络走法"（D52 / architecture.md 的【v2】3）。"""
    try:
        bases, active = settings_service.llm_config(session)
        for item in json.loads(bases):
            if item.get("name") == active:
                return item.get("proxy") or ""
    except Exception:  # noqa: BLE001 —— 拿不到就按"跟随系统"处理
        logger.warning("读不到聊天 provider 的 proxy，搜索按默认走")
    return ""


def _web_search(session: Session, friend_id: str, args: dict[str, Any]) -> dict[str, Any]:
    """联网搜一下（T2.0-7）。结果带编号 [1][2]，模型照着编号引用；来源由上层落库给前端。"""
    query = _text(args.get("query") or args.get("q") or args.get("keywords"))
    if not query:
        raise ValueError("web_search 要一个 query（要查的关键词）")
    payload = web_search_service.search(query, proxy=_chat_proxy(session))
    return {
        "query": payload["query"],
        "results": payload["results"],
        "excerpts": payload["excerpts"],
        "cached": payload["cached"],
    }


def _search_knowledge(session: Session, friend_id: str, args: dict[str, Any]) -> dict[str, Any]:
    """在她手头的资料里找相关内容（T2.1-3 / D98）。找不到就明说 —— 别编。"""
    query = _text(args.get("query") or args.get("q"))
    if not query:
        raise ValueError("search_knowledge 要一个 query（要找什么）")
    hits = knowledge_service.retrieve(session, friend_id, query, limit=4, max_chars=2000)
    if not hits:
        raise ValueError("你手头的资料里没找到相关的 —— 如实告诉 TA，别编")
    return {
        "query": query,
        "hits": [
            {
                "document": hit["document_name"],
                "text": hit["text"],
            }
            for hit in hits
        ],
    }


# ---------- v2.1（T2.1-4 / D99）：文件工具（读随便读，写要点头）----------


def _path_arg(args: dict[str, Any], *names: str) -> str:
    for name in names:
        value = args.get(name)
        if value:
            return str(value)
    raise ValueError(f"要一个路径参数（{'/'.join(names)}）")


def _list_dir(session: Session, friend_id: str, args: dict[str, Any]) -> dict[str, Any]:
    return file_service.list_dir(_path_arg(args, "path", "dir", "directory"))


def _read_file(session: Session, friend_id: str, args: dict[str, Any]) -> dict[str, Any]:
    raw_limit = args.get("max_chars")
    limit = int(raw_limit) if isinstance(raw_limit, (int, float)) and raw_limit > 0 else 4000
    return file_service.read_file(_path_arg(args, "path", "file"), max_chars=min(20000, limit))


def _write_file(session: Session, friend_id: str, args: dict[str, Any]) -> dict[str, Any]:
    content = args.get("content")
    if content is None:
        raise ValueError("write_file 要 path 和 content 两样")
    return file_service.write_file(_path_arg(args, "path", "file"), str(content))


def _move_path(session: Session, friend_id: str, args: dict[str, Any]) -> dict[str, Any]:
    return file_service.move_path(
        _path_arg(args, "src", "source", "from"), _path_arg(args, "dst", "target", "to")
    )


def _delete_path(session: Session, friend_id: str, args: dict[str, Any]) -> dict[str, Any]:
    return file_service.delete_path(_path_arg(args, "path", "file", "target"))


def describe_mutation(name: str, args: dict[str, Any]) -> tuple[str, str]:
    """给审批卡片看的 `(summary, detail)` —— **要说人话**，用户得看懂发生了什么。"""
    try:
        if name == "write_file":
            path = file_service.resolve_path(_path_arg(args, "path", "file"))
            detail = file_service.summarize_write(path, str(args.get("content") or ""))
            return f"她想把内容写进这个文件：{path}", detail
        if name == "move_path":
            source = file_service.resolve_path(_path_arg(args, "src", "source", "from"))
            target = file_service.resolve_path(_path_arg(args, "dst", "target", "to"))
            return f"她想把这个东西挪个位置：{source}", f"挪到：{target}"
        if name == "delete_path":
            path = file_service.resolve_path(_path_arg(args, "path", "file", "target"))
            kind = "目录" if path.is_dir() else "文件"
            return f"她想删掉这个{kind}：{path}", "删了就回不来了"
    except Exception as exc:  # noqa: BLE001 —— 参数有问题也照样要问用户（他会看到原因）
        return f"她想执行 {name}（参数有点问题：{exc}）", json.dumps(args, ensure_ascii=False)[:200]
    return f"她想执行 {name}", json.dumps(args, ensure_ascii=False)[:200]


@dataclass(frozen=True)
class ToolSpec:
    name: str
    summary: str  # 给 system prompt 的一行说明
    handler: Callable[[Session, str, dict[str, Any]], dict[str, Any]]
    # v2（T2.0-8 / prompts.md【v2】2）：**在哪些 mode 的清单里出现**。
    # 注意这**不是功能开关** —— 只是"给模型看哪些说明"；用户想要的能力切到工具模式就有。
    modes: tuple[str, ...] = ("normal", "roleplay", "tool")
    # v2.1（D99）：**会改动磁盘**的工具 → 执行前要用户点头（除非用户关了那个开关）
    mutating: bool = False
    # v2.2（D117）：慢工具（画图要 7~30 秒）走**异步** handler —— 别把事件循环堵住
    async_handler: Callable[..., Any] | None = None
    # v2.2 收尾（D136）：**同步但慢**的工具（搜网 2~6 秒 / 文件 IO / 知识库检索）
    # 都标记 blocking → `execute_async` 会把它们扔进线程跑，事件循环该干嘛干嘛
    # （不然一轮搜网就把整个后端的 SSE、看屏、语音全都卡住）。
    blocking: bool = False
    # v5.0（P3 / D250）：只在**这个朋友正在玩游戏**时才出现在工具清单里
    game_only: bool = False
    # v5.1（D290）：只有用户在游戏模式里手动开启作弊后才出现
    cheat_only: bool = False


# 加工具只改这张表：system prompt 的清单、执行分派、tool_result 全都跟着它走。
# M4 加 switch_mode / switch_role / adjust_rp_playstyle；M5 加 reminder 系列。
def _reference_image_bytes(session: Session, friend_id: str, args: dict[str, Any]) -> bytes | None:
    """`draw_image(ref=图片编号)` 的参考图（D129）：从历史里按编号找那张图，读成字节。

    编号跟 `look_image(no)` / 占位 `[图片#12]` 是同一套。找不到就给一句人话（别静默画成别的）。
    """
    raw = args.get("ref", args.get("ref_no", args.get("参考图")))
    if raw in (None, "", 0, "0"):
        return None
    try:
        number = int(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError("ref 要给图片编号（就是 [图片#12] 里那个数字）") from exc
    message = session.execute(
        select(Message).where(Message.friend_id == friend_id, Message.image_no == number)
    ).scalar_one_or_none()
    if message is None or not message.image_path:
        raise ValueError(f"没有第 {number} 张图（编号可能记错了）")
    from backend.services import image_service  # noqa: PLC0415 —— 懒导入

    path = image_service.abs_path(message.image_path)
    if path is None or not path.is_file():
        raise ValueError(f"第 {number} 张图的文件不在了")
    return path.read_bytes()


async def _draw_image(session: Session, friend_id: str, args: dict[str, Any]) -> dict[str, Any]:
    """画一张图（v2.2 / T2.2-5 / D117；带参考图 = 图生图，D129）。**慢**：走异步，别堵事件循环。"""
    from backend.services import imagegen_service  # noqa: PLC0415 —— 懒导入

    kwargs = args if isinstance(args, dict) else {}
    prompt = str(kwargs.get("prompt") or kwargs.get("描述") or "").strip()
    if not prompt:
        raise ServiceError("IMAGE_PROMPT_EMPTY", "你想画什么？给我一句画面描述就行", 400)
    ref_png = _reference_image_bytes(session, friend_id, kwargs)
    result = await imagegen_service.generate(prompt, session=session, ref_png=ref_png)
    return {
        "ok": True,
        "image_rel": result["rel"],
        "width": result["width"],
        "height": result["height"],
        "hint": "图已经画好，就在你这条回复下面（别在正文里贴路径或链接）",
    }


def _save_thought(session: Session, friend_id: str, args: dict[str, Any]) -> dict[str, Any]:
    """给自己留一张小纸条（D192）：**有寿命的一次性惦记**（跟 save_memory 不是一回事）。

    只准留四类（写死在工具说明里）：答应了的事 / 他在意的事有下文 / 刚想说又咽回去的想法 /
    有明确时间点的。同一件事只会有一张（自动合并），同时最多 3 张（满了丢最旧的随缘型）。
    """
    from backend.services import thought_service  # noqa: PLC0415 —— 懒导入

    kwargs = args if isinstance(args, dict) else {}
    content = str(kwargs.get("content") or kwargs.get("内容") or "").strip()
    thought, merged = thought_service.add(
        session,
        friend_id,
        content=content,
        kind=str(kwargs.get("kind") or kwargs.get("类型") or "casual"),
        due_at=kwargs.get("due_at") or kwargs.get("时间"),
        source="model",
    )
    return {
        "ok": True,
        "merged": merged,
        "kind": thought.kind,
        "hint": "这件事你本来就在惦记 —— 已经更新成最新的说法了"
        if merged
        else "纸条收好了：到时机（或随缘某个空档）我会提醒自己说。",
    }


def _reflect_speech(session: Session, friend_id: str, args: dict[str, Any]) -> dict[str, Any]:
    """把 TA 对说话方式的明确反馈写进长期档案（D347）。"""
    changes = args.get("changes")
    summary = str(args.get("summary") or "").strip() or None
    result = style_profile_service.apply_changes(
        session,
        friend_id,
        changes,
        summary=summary,
        reason="explicit_feedback",
    )
    return {
        "ok": True,
        "version": result["version"],
        "applied": result["applied"],
        "note": (
            "已经写进你的说话方式档案，下一轮自然照着说；"
            "不要把这条档案念给 TA，也不要写成长篇自我反省。"
        ),
    }


async def _look_screen(session: Session, friend_id: str, args: dict[str, Any]) -> dict[str, Any]:
    """看一眼 TA 现在的屏幕（2026-09-27 用户要求："自己截图自己看"这种能力要有）。

    链路跟「看看屏幕」那个按钮**完全一样**（D100）：本地截屏 → 交给**视觉模型**写成一句
    "他现在在干什么" → 把**那句话**回给模型（截图本身不进对话、不落库）。
    一轮对话最多看一次（护栏在 loop 那边传 `allow_screen_lookup`，不是时间冷却）。
    """
    from backend.services import screen_service  # noqa: PLC0415 —— 懒导入（它有桌面/网络依赖）

    png = await asyncio.to_thread(screen_service.capture)   # ImageGrab 是同步的，扔线程跑
    text = await screen_service.observe(png, session=session)
    return {
        "ok": True,
        "observation": text,
        "hint": "这就是你刚看到的那一眼（一句话）。照着它自然接话，别在正文里说'我看了屏幕'。",
    }


# ---------- v5.0（P3 / D250）：游戏工具 ----------
# 只在"这个朋友正在玩"时出现（`game_only=True`）。工具本身只转达，执行在适配器。


def _async_only(name: str) -> Callable[..., dict[str, Any]]:
    def _raise(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        raise ServiceError("ASYNC_ONLY", f"{name} 要走异步", 500)

    return _raise


def _game_manager():  # noqa: ANN202
    from backend.game.manager import get_manager  # noqa: PLC0415 —— 懒导入，避免循环

    return get_manager()


def _require_game(friend_id: str) -> None:
    if not _game_manager().active_for(friend_id):
        raise ValueError("现在没有在玩游戏 —— 先和 TA 一起开局")


async def _game_status(
    _session: Session, friend_id: str, _args: dict[str, Any]
) -> dict[str, Any]:
    _require_game(friend_id)
    manager = _game_manager()
    snapshot: dict[str, Any] = {}
    try:
        snapshot = await manager.agent_snapshot()
    except Exception as exc:  # noqa: BLE001 —— 快照失败也要把状态给她
        snapshot = {"error": str(exc)}
    return {"session": manager.status(), "snapshot": snapshot}


async def _game_inspect(
    _session: Session, friend_id: str, args: dict[str, Any]
) -> dict[str, Any]:
    _require_game(friend_id)
    center = args.get("center")
    if isinstance(center, (list, tuple)) and len(center) == 3:
        center = {"x": center[0], "y": center[1], "z": center[2]}
    if center is not None and (
        not isinstance(center, dict)
        or not all(axis in center for axis in ("x", "y", "z"))
    ):
        logger.warning("game_inspect center 不合法，回退到玩家脚下：{!r}", center)
        center = None
    try:
        radius = int(args.get("radius") or 4)
        height = int(args.get("height") or 4)
    except (TypeError, ValueError) as exc:
        raise ValueError("game_inspect 的 radius / height 必须是整数") from exc
    return await _game_manager().inspect_area(
        center=center,
        radius=radius,
        height=height,
        include_air=bool(args.get("include_air")),
    )


async def _game_survey(
    _session: Session, friend_id: str, args: dict[str, Any]
) -> dict[str, Any]:
    _require_game(friend_id)
    try:
        radius = int(args.get("radius") or 64)
        step = int(args.get("step") or 4)
    except (TypeError, ValueError) as exc:
        raise ValueError("game_survey 的 radius / step 必须是整数") from exc
    return await _game_manager().survey_area(radius=radius, step=step)


async def _game_do(
    _session: Session, friend_id: str, args: dict[str, Any]
) -> dict[str, Any]:
    _require_game(friend_id)
    manager = _game_manager()
    # 模型偶尔把整个 tool call 又包了一层（{"arguments": {...}}）——兼容。
    if "action" not in args and isinstance(args.get("arguments"), (dict, str)):
        nested = args.get("arguments")
        if isinstance(nested, str):
            try:
                nested = json.loads(nested)
            except json.JSONDecodeError:
                nested = None
        if isinstance(nested, dict):
            args = nested
    action = str(args.get("action") or "").strip()
    if not action:
        raise ValueError("game_do 需要 action")
    if action == "patrol":
        raise ValueError("patrol 是系统自动待机巡逻，不要主动调用")
    if action == "command":
        raise ValueError(
            "[条件不满足] command 只能由 game_cheat 在 TA 开启作弊后调用"
        )
    if (
        action not in ("say", "stop", "mode", "follow")
        and manager.world_actions_blocked()
        and not manager.incident_action_allowed(action)
    ):
        raise ValueError(
            "[条件不满足] TA 刚明确让你停下；世界动作保持锁定，"
            "等 TA 说继续或重新下指令。"
        )
    if (
        action not in ("say", "stop", "mode", "follow")
        and not manager.has_active_plan()
        and not manager.incident_action_allowed(action)
    ):
        raise ValueError(
            "[条件不满足] 世界动作必须放在 plan 里；先用 game_plan 创建或更新计划，"
            "不要裸调 game_do。读状态 / 说话 / 切模式不受此限制。"
        )
    raw_params = args.get("args")
    if isinstance(raw_params, str):
        try:
            raw_params = json.loads(raw_params)
        except json.JSONDecodeError:
            raw_params = None
    params = dict(raw_params) if isinstance(raw_params, dict) else {}
    # 模型偶尔把参数放在顶层（尤其 sequence 的 steps）——兼容，别让它空转失败
    for key, value in args.items():
        if key in ("action", "args"):
            continue
        params.setdefault(key, value)
    def _num(value: Any) -> bool:
        try:
            float(value)
            return True
        except (TypeError, ValueError):
            return False

    if action == "mine" and not (params.get("block") or params.get("item")):
        raise ValueError(
            '[格式错误] mine 需要 block，例如 {"action":"mine",'
            '"args":{"block":"oak_log","count":8}}'
        )
    if action == "goto" and not all(
        _num(params.get(axis)) for axis in ("x", "y", "z")
    ):
        raise ValueError(
            '[格式错误] goto 需要数字 x / y / z，例如 {"action":"goto",'
            '"args":{"x":0,"y":64,"z":0}}'
        )
    if action == "place":
        if not params.get("block") or not all(
            _num(params.get(axis)) for axis in ("x", "y", "z")
        ):
            raise ValueError(
                '[格式错误] place 需要 block + 数字 x / y / z，例如 '
                '{"action":"place","args":{"block":"dirt","x":0,"y":64,"z":0}}'
            )
    if action in ("craft", "smelt", "equip", "drop", "bring") and not (
        params.get("item") or params.get("block")
    ):
        raise ValueError(
            f'[格式错误] {action} 需要 item，例如 '
            f'{{"action":"{action}","args":{{"item":"oak_log","count":1}}}}'
        )
    if action == "sequence":
        steps = params.get("steps")
        if isinstance(steps, str):
            try:
                steps = json.loads(steps)
            except json.JSONDecodeError:
                steps = None
        if isinstance(steps, dict) and isinstance(steps.get("steps"), list):
            steps = steps["steps"]
        if isinstance(steps, list):
            normalized: list[dict[str, Any]] = []
            for step in steps:
                if isinstance(step, str):
                    try:
                        step = json.loads(step)
                    except json.JSONDecodeError:
                        continue
                if not isinstance(step, dict):
                    continue
                step_args = step.get("args")
                if isinstance(step_args, str):
                    try:
                        step_args = json.loads(step_args)
                    except json.JSONDecodeError:
                        step_args = {}
                normalized.append({**step, "args": step_args if isinstance(step_args, dict) else {}})
            params["steps"] = normalized
        if not isinstance(params.get("steps"), list) or not params["steps"]:
            raise ValueError(
                "sequence 需要 steps 数组（放在 args.steps 里），"
                '例如 {"action":"sequence","args":{"steps":[{"action":"mine",'
                '"args":{"block":"oak_log","count":8}}]}}'
            )
    return await manager.command(action, params, source="agent")


async def _game_say(
    _session: Session, friend_id: str, args: dict[str, Any]
) -> dict[str, Any]:
    _require_game(friend_id)
    text = str(args.get("text") or "").strip()
    if not text:
        raise ValueError("game_say 需要 text")
    return await _game_manager().command("say", {"text": text}, source="agent")


async def _game_cancel(
    _session: Session, friend_id: str, args: dict[str, Any]
) -> dict[str, Any]:
    _require_game(friend_id)
    task_id = str(args.get("task_id") or "").strip()
    if not task_id:
        raise ValueError("game_cancel 需要 task_id")
    return await _game_manager().cancel(task_id)


async def _game_plan(
    _session: Session, friend_id: str, args: dict[str, Any]
) -> dict[str, Any]:
    _require_game(friend_id)
    action = str(args.get("action") or "").strip().lower()
    if action:
        return _game_manager().plan_action(
            action,
            plan_id=str(args.get("plan_id") or "").strip() or None,
            condition=args.get("condition"),
            expected=args.get("expected"),
            result=args.get("result"),
            checks=args.get("checks"),
            tasks=args.get("tasks"),
            before=args.get("before"),
        )
    goal = str(args.get("goal") or args.get("text") or "").strip()
    raw_steps = args.get("steps")
    if isinstance(raw_steps, str):
        steps = [
            part.strip()
            for part in raw_steps.replace("；", "\n").replace(";", "\n").split("\n")
            if part.strip()
        ]
    elif isinstance(raw_steps, list):
        steps = [str(step) for step in raw_steps if str(step).strip()]
    else:
        steps = []
    done = bool(args.get("done"))
    if not goal and not done:
        raise ValueError("game_plan 需要 goal（或用 done=true 结束当前计划）")
    return _game_manager().set_plan(friend_id, goal, steps, done=done)


async def _game_mode(
    _session: Session, friend_id: str, args: dict[str, Any]
) -> dict[str, Any]:
    _require_game(friend_id)
    mode = str(args.get("mode") or "").strip().lower()
    if not mode:
        raise ValueError("game_mode 需要 mode（follow / chat / free）")
    return await _game_manager().set_mode(
        mode,
        player=args.get("player"),
        force=bool(args.get("force")),
    )


async def _game_cheat(
    _session: Session, friend_id: str, args: dict[str, Any]
) -> dict[str, Any]:
    _require_game(friend_id)
    action = str(args.get("action") or "").strip().lower()
    if not action:
        raise ValueError("game_cheat 需要 action")
    params = {
        key: value
        for key, value in args.items()
        if key != "action" and value is not None
    }
    try:
        return await _game_manager().cheat(action, params)
    except PermissionError as exc:
        raise ValueError(str(exc)) from exc


async def _game_skill(
    _session: Session, friend_id: str, args: dict[str, Any]
) -> dict[str, Any]:
    _require_game(friend_id)
    manager = _game_manager()
    if manager.world_actions_blocked():
        raise ValueError(
            "[条件不满足] TA 刚明确让你停下；技能执行保持锁定，"
            "等 TA 说继续或重新下指令。"
        )
    if not manager.has_active_plan():
        raise ValueError(
            "[条件不满足] 技能执行也是世界动作；先用 game_plan 创建任务，"
            "再在计划里执行技能。"
        )
    name = str(args.get("name") or "").strip()
    if not name:
        raise ValueError("game_skill 需要 name")
    params = args.get("args")
    if isinstance(params, str):
        try:
            params = json.loads(params)
        except json.JSONDecodeError as exc:
            raise ValueError("game_skill 的 args 不是合法 JSON") from exc
    if params is not None and not isinstance(params, dict):
        raise ValueError("game_skill 的 args 必须是对象")
    return await _game_manager().use_skill(name, params or {})


async def _game_build(
    _session: Session, friend_id: str, args: dict[str, Any]
) -> dict[str, Any]:
    _require_game(friend_id)
    action = str(args.get("action") or "").strip().lower()
    if not action:
        raise ValueError("game_build 需要 action")
    return await _game_manager().blueprint_action(action, args, preview_only=True)


async def _game_knowledge(
    _session: Session, friend_id: str, args: dict[str, Any]
) -> dict[str, Any]:
    _require_game(friend_id)
    query = str(args.get("query") or args.get("action") or "").strip()
    name = str(args.get("name") or args.get("item") or args.get("block") or "").strip()
    if query not in ("recipe_for", "tool_tier_for"):
        raise ValueError("game_knowledge query 只能是 recipe_for / tool_tier_for")
    return await _game_manager().knowledge_query(query, name)


async def _game_inventory(
    _session: Session, friend_id: str, args: dict[str, Any]
) -> dict[str, Any]:
    _require_game(friend_id)
    return await _game_manager().inventory_detail()


async def _game_plugin(
    _session: Session, friend_id: str, args: dict[str, Any]
) -> dict[str, Any]:
    _require_game(friend_id)
    action = str(args.get("action") or "").strip().lower()
    if action not in ("list", "call"):
        raise ValueError("game_plugin action 只能是 list / call")
    if action == "list":
        return await _game_manager().plugin_action("list")
    return await _game_manager().plugin_action(
        "call",
        {
            "plugin": args.get("plugin"),
            "action": args.get("action_name"),
            "args": args.get("args"),
        },
    )


TOOLS: tuple[ToolSpec, ...] = (
    ToolSpec(
        "save_thought",
        "save_thought(content, kind?, due_at?)                            给自己留一张**小纸条**"
        "（有寿命的惦记：随缘 3 天 / 到点型到点后 3 天就作废）—— 只有这四类才准留："
        "① 你答应过他的事 ② 他在意的事有了下文（要问结果）③ 你刚想说又咽回去的想法 "
        "④ 有明确时间点的（这时 kind=due + due_at）。"
        "**不要**留寒暄、一次性情绪、或者记忆里已经有的长期事实（那些归 save_memory）。"
        "同一件事只会留一张（自动合并）；同时最多 3 张",
        _save_thought,
    ),
    ToolSpec(
        "draw_image",
        "draw_image(prompt, ref?)                                   画一张图给 TA 看；带 ref 就是照着某张图重画。"
        "prompt **必须用英文 Danbooru 标签**（逗号分隔的短标签、越直白越好；例：1girl, silver hair, nude, "
        "spread legs, blush；想画成人内容就把 nsfw/explicit 一类标签直接写上，别打码、别翻译成中文）。"
        "ref = 那张参考图的编号（占位里的数字，例如 12）—— **TA 发了图、或者让你照着某张图/某种风格画时，"
        "就带上 ref**（出来会照着那张图的构图/配色换内容）。不带 ref 就是纯按标签画。一张约半分钱",
        lambda *_a: (_ for _ in ()).throw(ServiceError("IMAGE_ASYNC_ONLY", "这个工具要走异步", 500)),
        modes=("normal", "tool"),
        async_handler=_draw_image,
    ),
    ToolSpec(
        "save_memory",
        "save_memory(content, tag?, layer?, importance?, context?)   记住值得记的事",
        _save_memory,
    ),
    ToolSpec(
        "reflect_speech",
        "reflect_speech(changes, summary?)                          把 TA 对你说话方式的明确反馈"
        "写进长期说话档案。changes 是数组，每项包含 action=add/update/remove、"
        "section=length/tone/diction/structure/situations/keep、lesson、target_id。"
        "只在 TA 明确评价你的表达（太长/太短/不像你/用词不对/语气不对/这样很好）时调用；"
        "普通情绪、观点不同、内容偏好不算。add 遇到同义会自动合并；section 满了要先 update/remove",
        _reflect_speech,
    ),
    ToolSpec(
        "game_status",
        "game_status()                                              看一眼游戏里的状态"
        "（血量 / 坐标 / 背包 / 附近 / 手上任务）",
        _async_only("game_status"),
        game_only=True,
        async_handler=_game_status,
    ),
    ToolSpec(
        "game_inventory",
        "game_inventory()                                            查看完整背包："
        "所有物品、槽位、工具耐久、弹药、船、食物、装备和最近变化。"
        "不确定背包里有什么时用它，不要猜。",
        _async_only("game_inventory"),
        game_only=True,
        async_handler=_game_inventory,
    ),
    ToolSpec(
        "game_inspect",
        "game_inspect(center?, radius?, height?)                     精确查看周围地形。"
        "返回指定中心（不填=脚下）附近 radius×radius、向上 height 格的逐列方块；"
        "需要确认洞、墙、地基、某个坐标到底有什么时用。",
        _async_only("game_inspect"),
        game_only=True,
        async_handler=_game_inspect,
    ),
    ToolSpec(
        "game_survey",
        "game_survey(radius?, step?)                                  大范围紧凑地形图。"
        "返回周围俯视字符图、高度范围、最近水/岩浆/树/石地。"
        "radius=8~128，step=2~16；需要找湖、找平地、判断大范围地形时用。",
        _async_only("game_survey"),
        game_only=True,
        async_handler=_game_survey,
    ),
    ToolSpec(
        "game_do",
        "game_do(action, args?)                                      在 active plan 内执行一步"
        "（必须先有 game_plan 创建的 executing/paused plan；"
        "action 只能用当前适配器支持的动作：mine / place / attack / eat / "
        "goto / drop / bring / escape / pillar / craft / smelt / equip / sequence / "
        "fly / fish / shoot / boat / mount / dismount / use_item / "
        "rest / wait；跟随/聊天/自由用 game_mode，不要用 game_do(follow)）。"
        " 定量动作必须带 count（例如 mine 32 块木头）。"
        '例：{"action":"mine","args":{"block":"dirt"}}；'
        '{"action":"mine","args":{"block":"oak_log","count":32}}；'
        '{"action":"bring","args":{"item":"dirt","count":1}}；'
        '{"action":"sequence","args":{"steps":[{"action":"mine","args":{"block":"oak_log","count":8}},'
        '{"action":"craft","args":{"item":"oak_planks","count":32}}]}}'
        '（sequence 的 steps 必须放在 args.steps 里）；'
        '{"action":"rest","args":{"seconds":30}}；'
        '{"action":"wait","args":{"seconds":60}}；'
        '{"action":"escape"}。'
        "失败时先看错误：格式错误（[格式错误]/需要 steps/需要 block/需要 x y z）"
        "→ 按示例改好再试；世界状态错误（没吃的/没找到/路被挡）→ 换动作，不要原样重试。",
        _async_only("game_do"),
        game_only=True,
        async_handler=_game_do,
    ),
    ToolSpec(
        "game_say",
        "game_say(text)                                              在游戏聊天里说一句",
        _async_only("game_say"),
        game_only=True,
        async_handler=_game_say,
    ),
    ToolSpec(
        "game_cancel",
        "game_cancel(task_id)                                        取消一个游戏任务"
        "（id 来自 game_do 的返回）",
        _async_only("game_cancel"),
        game_only=True,
        async_handler=_game_cancel,
    ),
    ToolSpec(
        "game_plan",
        "game_plan(action, plan_id?, condition?, expected?, result?, tasks?, before?)  "
        "管理 plan/task：action=create/update/add_tasks/pause/resume/cancel/complete/list。"
        "所有会改变世界的动作都必须放进 plan；game_do / game_skill 没有 active plan 会被拒绝。"
        "plan 有 condition（前置条件）/expected（预期结论）/result（实际结果），"
        "还可以带 checks（结构化后置条件，如 inventory_has / block_at / block_count / "
        "entity_near / position_near / health / food / task_result）；"
        "不会失败；task 是可执行小动作，会 success/failed。"
        "条件不足就用 add_tasks 把补条件的 task 插到前面。"
        "（旧参数 goal/steps/done 仍兼容）",
        _async_only("game_plan"),
        game_only=True,
        async_handler=_game_plan,
    ),
    ToolSpec(
        "game_mode",
        "game_mode(mode, force?, player?)                            切换游戏常态模式："
        "follow=跟着 TA，chat=待在 TA 附近聊天，free=自由活动（**只有 free 能自己给自己派活**）。"
        "不要频繁切换；TA 明确要求时才 force=true",
        _async_only("game_mode"),
        game_only=True,
        async_handler=_game_mode,
    ),
    ToolSpec(
        "game_cheat",
        "game_cheat(action, ...)                                    使用 TA 手动开启的作弊能力"
        "（command / give / teleport / gamemode / time / weather / heal / feed / "
        "instant_mine / fly / no_damage / reveal_map）。"
        "只有总开关和这一项都开着才会出现；你**不能自己开启**，也不要反复要求 TA 开。",
        _async_only("game_cheat"),
        game_only=True,
        cheat_only=True,
        async_handler=_game_cheat,
    ),
    ToolSpec(
        "game_skill",
        "game_skill(name, args?)                                      在 active plan 内执行已学会技能"
        "（技能书里启用的；会按步骤跑完并做后置条件校验，不额外调 LLM）。",
        _async_only("game_skill"),
        game_only=True,
        async_handler=_game_skill,
    ),
    ToolSpec(
        "game_build",
        "game_build(action, material?, ...)                           设计建造稿，不自动建 plan。"
        "action=hut 时按 TA 指定的 material（如 dirt / oak_planks）生成小屋蓝图："
        "系统自己算材料、找平地、铺地板、围墙留门洞、盖屋顶并逐块校验；"
        "hut 只是参考模板，你可以在 blueprint 里用 size+palette+parts 自由设计形状。"
        "action=hut/build 只返回 blueprint、材料缺口、选址和 plan_tasks；"
        "要施工必须由你再用 game_plan 创建/更新计划。"
        "同一个蓝图已经有 active plan 时会继续原 plan（原位补齐），"
        "只有你明确要另起一座时才传 new_plan=true。"
        "也支持 design/save/list/build 和自由创作记录。",
        _async_only("game_build"),
        game_only=True,
        async_handler=_game_build,
    ),
    ToolSpec(
        "game_knowledge",
        "game_knowledge(query, name)                                   从 minecraft-data 查事实："
        "recipe_for(item) 配方 / tool_tier_for(block) 所需工具等级。"
        "不要凭记忆猜配方或镐子等级。",
        _async_only("game_knowledge"),
        game_only=True,
        async_handler=_game_knowledge,
    ),
    ToolSpec(
        "game_plugin",
        "game_plugin(action, plugin?, action_name?, args?)              调用模组插件桥。"
        "action=list 看装了哪些插件 / 是否可用；action=call 调具体插件动作。"
        "插件缺模组时会安全跳过，不要反复试。",
        _async_only("game_plugin"),
        game_only=True,
        async_handler=_game_plugin,
    ),
    ToolSpec(
        "get_time",
        "get_time()                                                  现在几点（要判断时间、算了多久、"
        "该不该催他睡觉这类事时用）",
        _get_time,
    ),
    ToolSpec(
        "update_memory",
        "update_memory(id, content?, importance?, layer?)            改内容 / 重要度 / **层（升·降级）**（id 来自 recall）",
        _update_memory,
    ),
    ToolSpec(
        "delete_memory",
        "delete_memory(id)                                           删一条（**软删**，30 天可恢复）（id 来自 recall）",
        _delete_memory,
    ),
    ToolSpec(
        "recall_memory",
        "recall_memory(query)                                        想不起来时去翻记忆（**关键词 + 语义**）",
        _recall_memory,
    ),
    ToolSpec(
        "switch_mode",
        "switch_mode(mode)                                          入戏/出戏（mode = normal|roleplay|tool）",
        _switch_mode,
    ),
    ToolSpec(
        "switch_role",
        "switch_role(role)                                          换角色（会自动进入入戏模式）",
        _switch_role,
    ),
    ToolSpec(
        "adjust_rp_playstyle",
        "adjust_rp_playstyle(text)                                  调整当前角色的演法（同名覆盖）",
        _adjust_rp_playstyle,
    ),
    ToolSpec(
        "add_schedule",
        "add_schedule(title, date, time?, nature?, owner?)           记一件事到日程（你也用这个记你自己的事）；"
        "date=YYYY-MM-DD 或 10m/2h/1d；time=HH:MM 不填就只到日（不响，你找机会提）",
        _add_schedule,
    ),
    ToolSpec(
        "list_schedules",
        "list_schedules(query?, owner?, include_settled?)            看日程清单（默认只看还没结束的；owner=friend 只看你自己的）",
        _list_schedules,
    ),
    ToolSpec(
        "update_schedule",
        "update_schedule(id? 或 title, date?, time?, state?, reason?) 改期 / 改内容 / 标完成"
        "（id/title 只是用来找那条；改名用 new_title。state=settled 表示做了或不做了；"
        "time 传空字符串 = 改回只到日）",
        _update_schedule,
    ),
    ToolSpec(
        "remove_schedule",
        "remove_schedule(id? 或 title)                               删掉一条日程",
        _remove_schedule,
    ),
    ToolSpec(
        "look_image",
        "look_image(no? 或 query?)                                   看一眼旧图：no=编号（[图片#12] 里的 12）"
        "或 latest（不填=最近一张）；也可以直接给一个词（query，比如「猫」——在图片索引里找）",
        _look_image,
    ),
    ToolSpec(
        "list_images",
        "list_images(limit?)                                        翻相册：最近发过的图（编号 / 本地时间 / 谁发的 / 我记下的一句话内容）",
        _list_images,
    ),
    ToolSpec(
        "look_screen",
        "look_screen()                                               看一眼 TA 现在的电脑屏幕"
        "（截一张，交给视觉模型写成一句话给你）—— 想知道他现在在干什么、或者他让你看屏幕时用。"
        "**一轮对话最多看一次**；只能拿到一句转述，拿不到图本身",
        lambda *_a: (_ for _ in ()).throw(ServiceError("SCREEN_ASYNC_ONLY", "这个工具要走异步", 500)),
        modes=("normal", "tool"),
        async_handler=_look_screen,
    ),
    ToolSpec(
        "web_search",
        "web_search(query)                                           需要查证的事（新闻/事实/价格/天气）联网搜一下，回答里句末标来源编号 [1]",
        _web_search,
        # 搜网一搜就出戏 → 入戏模式不给它看（prompts.md【v2】2）
        modes=("normal", "tool"),
        blocking=True,  # 网络 2~6 秒（D136）
    ),
    ToolSpec(
        "search_knowledge",
        "search_knowledge(query)                                      在 TA 给你的资料里找内容（找不到就说没写，别编）",
        _search_knowledge,
        # 跟搜网同一类（会往对话里灌外部内容）→ 入戏不给，免得出戏
        modes=("normal", "tool"),
        blocking=True,  # 资料多时切词打分是 CPU + DB 活（D136）
    ),
    ToolSpec(
        "list_dir",
        "list_dir(path)                                              看看某个目录里有什么（绝对路径；只读，不问）",
        _list_dir,
        modes=("normal", "tool"),
        blocking=True,  # 磁盘 IO（可能是机械盘 / 网络盘，D136）
    ),
    ToolSpec(
        "read_file",
        "read_file(path, max_chars?)                                 读一个文本文件（绝对路径；只读，不问）",
        _read_file,
        modes=("normal", "tool"),
        blocking=True,
    ),
    ToolSpec(
        "write_file",
        "write_file(path, content)                                   写/覆盖一个文件（**要用户点头**）",
        _write_file,
        modes=("normal", "tool"),
        mutating=True,
        blocking=True,
    ),
    ToolSpec(
        "move_path",
        "move_path(src, dst)                                         挪动文件/空目录（**要用户点头**）",
        _move_path,
        modes=("normal", "tool"),
        mutating=True,
        blocking=True,
    ),
    ToolSpec(
        "delete_path",
        "delete_path(path)                                           删文件或空目录（**要用户点头**；不递归删）",
        _delete_path,
        modes=("normal", "tool"),
        mutating=True,
        blocking=True,
    ),
)

_BY_NAME: dict[str, ToolSpec] = {tool.name: tool for tool in TOOLS}


# ── v3.8（D169）：**原生 function calling** 的参数 schema（OpenAI 风格）────────────
#
# 为什么要有它：能力声明里 `native_tool_calls: true` 的模型，可以像 OpenAI 那样**原生**调工具
# （`tools=[...]` + 流式 `tool_calls`）—— 比让它手写 `<tool>{...}</tool>` 可靠得多，
# 直接决定"她到底会不会自己去做事"（用户反馈的"程序感、自主意识弱"里最实在的一环）。
# 加工具时**这里也要加一条**；没写 schema 的工具不会进原生清单，但仍走提示词协议（D14 兜底）。
NATIVE_SCHEMAS: dict[str, dict[str, Any]] = {
    "draw_image": {
        "type": "object",
        "properties": {
            "prompt": {
                "type": "string",
                "description": "英文 Danbooru 标签（逗号分隔，越直白越好；成人向直接写 nsfw/explicit）",
            },
            "ref": {"type": "integer", "description": "参考图编号（[图片#12] 里的 12）；要照着某张图重画时才带"},
        },
        "required": ["prompt"],
    },
    "save_thought": {
        "type": "object",
        "properties": {
            "content": {"type": "string", "description": "一句话：你想跟他说的那件事"},
            "kind": {
                "type": "string",
                "enum": ["casual", "due"],
                "description": "casual=随缘（3 天内找机会说）；due=有明确时间点（必须给 due_at）",
            },
            "due_at": {
                "type": "string",
                "description": "kind=due 时：什么时候说（ISO8601，如 2026-09-28T09:00:00+08:00）",
            },
        },
        "required": ["content"],
    },
    "save_memory": {
        "type": "object",
        "properties": {
            "content": {"type": "string", "description": "要记住的事（完整一句话，别用代词）"},
            # v3.55（D242）：原来写"如 用户偏好 / 关系 / 日常" → 她就照着自己编
            # （实测写过 `梦汐·关系·自我定位`，被 _normalize_tag 落成 normal + WARNING）。
            # tag 的合法值只有 normal/rp/rp_play_style/notebook，一般**不用填**。
            "tag": {"type": "string", "description": "一般不用填（默认 normal）"},
            "layer": {
                "type": "string",
                "enum": ["pinned", "permanent", "long_term", "short_term"],
                "description": "pinned=小本本（最不能忘）/ permanent=长期事实 / long_term=一般记忆",
            },
            "importance": {"type": "number", "description": "0~1，越重要越大"},
            "context": {"type": "string", "description": "当时是什么情景"},
        },
        "required": ["content"],
    },
    "reflect_speech": {
        "type": "object",
        "properties": {
            "changes": {
                "type": "array",
                "description": "要写入说话档案的 1~6 条改动",
                "items": {
                    "type": "object",
                    "properties": {
                        "action": {
                            "type": "string",
                            "enum": ["add", "update", "remove"],
                        },
                        "section": {
                            "type": "string",
                            "enum": [
                                "length",
                                "tone",
                                "diction",
                                "structure",
                                "situations",
                                "keep",
                            ],
                        },
                        "lesson": {
                            "type": "string",
                            "description": "一句可执行的规则，最多 60 字（add/update 必填）",
                        },
                        "target_id": {
                            "type": "string",
                            "description": "要改/删的旧规则 id（update/remove 必填）",
                        },
                        "why": {"type": "string", "description": "为什么改（可空）"},
                    },
                    "required": ["action", "section"],
                },
            },
            "summary": {"type": "string", "description": "一句话概括这次学到什么（可空）"},
        },
        "required": ["changes"],
    },
    "get_time": {
        "type": "object",
        "properties": {},
        "description": "现在几点（要判断时间、算了多久、该不该催他睡觉时用）",
    },
    "update_memory": {
        "type": "object",
        "properties": {
            "id": {"type": "string", "description": "记忆 id（来自 recall_memory）"},
            "content": {"type": "string", "description": "新的内容"},
            "importance": {"type": "number", "description": "0~1"},
            "layer": {
                "type": "string",
                "enum": ["pinned", "permanent", "long_term", "short_term"],
                "description": "改成哪一层（升 / 降级）",
            },
        },
        "required": ["id"],
    },
    "delete_memory": {
        "type": "object",
        "properties": {"id": {"type": "string", "description": "记忆 id（来自 recall_memory）"}},
        "required": ["id"],
    },
    "recall_memory": {
        "type": "object",
        "properties": {"query": {"type": "string", "description": "想找什么（关键词）"}},
        "required": ["query"],
    },
    "switch_mode": {
        "type": "object",
        "properties": {
            "mode": {"type": "string", "enum": ["normal", "roleplay", "tool"], "description": "切到哪个模式"},
        },
        "required": ["mode"],
    },
    "switch_role": {
        "type": "object",
        "properties": {"role": {"type": "string", "description": "要演的角色名"}},
        "required": ["role"],
    },
    "adjust_rp_playstyle": {
        "type": "object",
        "properties": {"text": {"type": "string", "description": "这个角色该怎么演（同名覆盖）"}},
        "required": ["text"],
    },
    "add_schedule": {
        "type": "object",
        "properties": {
            "title": {"type": "string", "description": "这件事是什么（一句话）"},
            "date": {
                "type": "string",
                "description": (
                    "哪天（**本地日期**，YYYY-MM-DD，例如 2026-10-09）；"
                    "「几分钟/几小时后」也可以写 10m / 2h"
                ),
            },
            "time": {
                "type": "string",
                "description": "几点（本地时刻 HH:MM，例如 15:30）。**不填 = 只到日**：不响，你自己找机会提",
            },
            "nature": {
                "type": "string",
                "enum": ["start", "due", "appointment", "remind"],
                "description": "性质：start=开始做 / due=截止 / appointment=约好的时点 / remind=纯提醒",
            },
            "advance_minutes": {"type": "integer", "description": "提前多少分钟提醒（默认 0）"},
            "owner": {
                "type": "string",
                "enum": ["user", "friend"],
                "description": "user=他的事（默认）/ friend=你自己的事",
            },
        },
        "required": ["title", "date"],
    },
    "list_schedules": {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "只找标题里带这个词的"},
            "owner": {"type": "string", "enum": ["user", "friend"], "description": "只看某一边的"},
            "include_settled": {"type": "boolean", "description": "连已结算的也一起看"},
        },
    },
    "update_schedule": {
        "type": "object",
        "properties": {
            "id": {"type": "string", "description": "日程 id（system prompt 的日程清单里有）"},
            "title": {"type": "string", "description": "或者用标题关键词找（**只是查找用，不会改标题**）"},
            "new_title": {"type": "string", "description": "要改标题时才用它"},
            "date": {"type": "string", "description": "改到哪天（YYYY-MM-DD）"},
            "time": {"type": "string", "description": "改到几点（HH:MM）；传空字符串 = 改回只到日"},
            "state": {
                "type": "string",
                "enum": ["settled", "pending"],
                "description": "settled=做了/不做了；pending=重开/改期",
            },
            "reason": {"type": "string", "enum": ["done", "dropped"], "description": "settled 时：做了 / 不做了"},
            "nature": {"type": "string", "enum": ["start", "due", "appointment", "remind"]},
            "advance_minutes": {"type": "integer"},
        },
    },
    "remove_schedule": {
        "type": "object",
        "properties": {
            "id": {"type": "string", "description": "日程 id"},
            "title": {"type": "string", "description": "或者用标题关键词找"},
        },
    },
    "look_image": {
        "type": "object",
        "properties": {
            "no": {
                "type": "string",
                "description": "图片编号（[图片#12] 里的那个 12），或 latest（最近一张）；不填也是最近一张",
            },
            "query": {"type": "string", "description": "或者给一个词，在图片内容索引里找（比如「猫」）"},
        },
    },
    "list_images": {
        "type": "object",
        "properties": {"limit": {"type": "integer", "description": "看最近几张（默认 10，最多 30）"}},
    },
    "look_screen": {
        "type": "object",
        "properties": {},
    },
    "web_search": {
        "type": "object",
        "properties": {"query": {"type": "string", "description": "搜什么（新闻/事实/价格/天气这类要查证的）"}},
        "required": ["query"],
    },
    "search_knowledge": {
        "type": "object",
        "properties": {"query": {"type": "string", "description": "在 TA 给的资料里找什么"}},
        "required": ["query"],
    },
    "list_dir": {
        "type": "object",
        "properties": {"path": {"type": "string", "description": "目录绝对路径（只读，不问）"}},
        "required": ["path"],
    },
    "read_file": {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "文件绝对路径（只读，不问）"},
            "max_chars": {"type": "integer", "description": "最多读多少字"},
        },
        "required": ["path"],
    },
    "write_file": {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "文件绝对路径（**要用户点头**）"},
            "content": {"type": "string", "description": "写什么（整份内容）"},
        },
        "required": ["path", "content"],
    },
    "move_path": {
        "type": "object",
        "properties": {
            "src": {"type": "string", "description": "源路径（**要用户点头**）"},
            "dst": {"type": "string", "description": "目标路径"},
        },
        "required": ["src", "dst"],
    },
    "delete_path": {
        "type": "object",
        "properties": {"path": {"type": "string", "description": "要删的路径（**要用户点头**；不递归删）"}},
        "required": ["path"],
    },
    "game_status": {"type": "object", "properties": {}, "required": []},
    "game_inventory": {"type": "object", "properties": {}, "required": []},
    "game_inspect": {
        "type": "object",
        "properties": {
            "center": {
                "type": "object",
                "properties": {
                    "x": {"type": "number"},
                    "y": {"type": "number"},
                    "z": {"type": "number"},
                },
            },
            "radius": {"type": "integer", "minimum": 1, "maximum": 8},
            "height": {"type": "integer", "minimum": 1, "maximum": 8},
            "include_air": {"type": "boolean"},
        },
        "required": [],
    },
    "game_survey": {
        "type": "object",
        "properties": {
            "radius": {"type": "integer", "minimum": 8, "maximum": 128},
            "step": {"type": "integer", "minimum": 2, "maximum": 16},
        },
        "required": [],
    },
    "game_do": {
        "type": "object",
        "properties": {
            "action": {"type": "string", "description": "适配器支持的动作名"},
            "args": {"type": "object", "description": "动作参数"},
        },
        "required": ["action"],
    },
    "game_say": {
        "type": "object",
        "properties": {"text": {"type": "string", "description": "要说的话"}},
        "required": ["text"],
    },
    "game_cancel": {
        "type": "object",
        "properties": {"task_id": {"type": "string", "description": "要取消的任务 id"}},
        "required": ["task_id"],
    },
    "game_plan": {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": [
                    "create",
                    "update",
                    "add_tasks",
                    "pause",
                    "resume",
                    "cancel",
                    "complete",
                    "get",
                    "list",
                ],
                "description": "plan 操作；不填则走旧参数 goal/steps/done",
            },
            "plan_id": {"type": "string", "description": "plan id（如 plan-1）"},
            "condition": {
                "anyOf": [{"type": "string"}, {"type": "object"}, {"type": "array"}],
                "description": "前置条件（文字或结构化数据），必须有",
            },
            "expected": {
                "anyOf": [{"type": "string"}, {"type": "object"}, {"type": "array"}],
                "description": "预期结论 / 验收标准，必须有",
            },
            "result": {
                "anyOf": [{"type": "string"}, {"type": "object"}, {"type": "array"}],
                "description": "实际结果（完成/取消时写）",
            },
            "checks": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "type": {
                            "type": "string",
                            "enum": [
                                "inventory_has",
                                "block_at",
                                "block_count",
                                "entity_near",
                                "position_near",
                                "health",
                                "food",
                                "task_result",
                            ],
                        },
                        "item": {"type": "string"},
                        "block": {"type": "string"},
                        "name": {"type": "string"},
                        "x": {"type": "number"},
                        "y": {"type": "number"},
                        "z": {"type": "number"},
                        "count": {"anyOf": [{"type": "integer"}, {"type": "string"}]},
                        "radius": {"type": "number"},
                        "distance": {"type": "number"},
                        "value": {"anyOf": [{"type": "number"}, {"type": "string"}, {"type": "boolean"}]},
                        "path": {"type": "string"},
                    },
                    "required": ["type"],
                },
                "description": "plan 全部 task 成功后的最终后置条件",
            },
            "tasks": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "action": {"type": "string"},
                        "args": {"type": "object"},
                        "checks": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "type": {"type": "string"},
                                    "item": {"type": "string"},
                                    "block": {"type": "string"},
                                    "name": {"type": "string"},
                                    "x": {"type": "number"},
                                    "y": {"type": "number"},
                                    "z": {"type": "number"},
                                    "count": {
                                        "anyOf": [{"type": "integer"}, {"type": "string"}]
                                    },
                                    "radius": {"type": "number"},
                                    "distance": {"type": "number"},
                                    "value": {
                                        "anyOf": [
                                            {"type": "number"},
                                            {"type": "string"},
                                            {"type": "boolean"},
                                        ]
                                    },
                                    "path": {"type": "string"},
                                },
                                "required": ["type"],
                            },
                            "description": "这个 task 结束后必须通过的条件",
                        },
                    },
                    "required": ["action"],
                },
                "description": "可执行的小动作列表（不要放 say/stop/mode/patrol）",
            },
            "before": {
                "type": "integer",
                "description": "add_tasks 插到第几个 task 前面（1-based；默认最前面）",
            },
            "goal": {"type": "string", "description": "你当前想完成的事（具体、可验收）"},
            "steps": {
                "type": "array",
                "items": {"type": "string"},
                "description": "2~5 个具体步骤（按顺序）",
            },
            "done": {
                "type": "boolean",
                "description": "true = 计划完成或放弃，清空当前计划",
            },
        },
        "required": [],
    },
    "game_mode": {
        "type": "object",
        "properties": {
            "mode": {
                "type": "string",
                "enum": ["follow", "chat", "free"],
                "description": "follow=跟着 TA；chat=待在 TA 附近聊天；free=自由活动（可以自己给自己派活）",
            },
            "force": {
                "type": "boolean",
                "description": "TA 明确要求换模式时才 true；否则默认受 5 分钟冷却限制",
            },
            "player": {
                "type": "string",
                "description": "要跟随/靠近的玩家名；不填 = 最近的玩家",
            },
        },
        "required": ["mode"],
    },
    "game_cheat": {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": [
                    "command",
                    "give",
                    "teleport",
                    "gamemode",
                    "time",
                    "weather",
                    "heal",
                    "feed",
                    "instant_mine",
                    "fly",
                    "no_damage",
                    "reveal_map",
                ],
                "description": "TA 已开启的作弊能力",
            },
            "command": {
                "type": "string",
                "description": "action=command 时执行的单条命令（不要带 /）",
            },
            "commands": {
                "type": "array",
                "items": {"type": "string"},
                "description": "action=command 时可选多条命令",
            },
            "item": {"type": "string", "description": "action=give 的物品 id"},
            "count": {"type": "integer", "description": "action=give 的数量"},
            "x": {"type": "number"},
            "y": {"type": "number"},
            "z": {"type": "number"},
            "mode": {
                "type": "string",
                "enum": ["survival", "creative", "adventure", "spectator"],
            },
            "value": {
                "type": "string",
                "description": "time 用 day/noon/night/midnight/tick；weather 用 clear/rain/thunder",
            },
            "duration": {
                "type": "integer",
                "description": "weather 持续时间（秒；可选）",
            },
        },
        "required": ["action"],
    },
    "game_skill": {
        "type": "object",
        "properties": {
            "name": {"type": "string", "description": "技能名（来自提示词里的技能书）"},
            "args": {
                "type": "object",
                "description": "技能参数；不填就用技能默认参数",
            },
        },
        "required": ["name"],
    },
    "game_build": {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": [
                    "hut",
                    "design",
                    "save",
                    "list",
                    "build",
                    "creative_start",
                    "creative_finish",
                    "creative_rate",
                    "status",
                ],
            },
            "material": {
                "type": "string",
                "description": "hut 的建造材料，直接取 TA 指定的方块 id（如 dirt / oak_planks）",
            },
            "width": {"type": "integer", "minimum": 3, "maximum": 7},
            "depth": {"type": "integer", "minimum": 3, "maximum": 7},
            "height": {"type": "integer", "minimum": 2, "maximum": 4},
            "blueprint": {
                "type": "object",
                "description": (
                    "自由设计：name/description/size/palette/parts（盒子），"
                    "或完整 layers。parts 用 x/y/z/width/height/depth + block，"
                    "可用 holes 挖门洞/窗/室内；hut 只是参考模板，不是唯一形状。"
                ),
            },
            "blueprint_id": {"type": "string"},
            "anchor": {
                "type": "object",
                "properties": {
                    "x": {"type": "number"},
                    "y": {"type": "number"},
                    "z": {"type": "number"},
                },
            },
            "new_plan": {
                "type": "boolean",
                "description": "true = 明确要另起一座同蓝图；默认复用已有 active plan",
            },
            "brief": {"type": "string", "description": "自由创作的创作目标"},
            "design": {
                "type": "object",
                "description": "草图 / 调色板 / 结构描述 / 蓝图之一",
            },
            "size": {
                "type": "object",
                "properties": {
                    "x": {"type": "integer"},
                    "y": {"type": "integer"},
                    "z": {"type": "integer"},
                },
            },
            "record_id": {"type": "string"},
            "rating": {"type": "integer", "minimum": 1, "maximum": 5},
        },
        "required": ["action"],
    },
    "game_knowledge": {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "enum": ["recipe_for", "tool_tier_for"],
            },
            "name": {
                "type": "string",
                "description": "物品 id（recipe_for）或方块 id（tool_tier_for）",
            },
        },
        "required": ["query", "name"],
    },
    "game_plugin": {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ["list", "call"]},
            "plugin": {"type": "string", "description": "插件 id，例如 inventory_bridge"},
            "action_name": {"type": "string", "description": "插件动作名"},
            "args": {"type": "object", "description": "插件动作参数"},
        },
        "required": ["action"],
    },
}


def _summary_description(summary: str) -> str:
    """把 manifest 的 `name(args)    说明` 拆出**说明**那半截（给原生 schema 当 description）。"""
    parts = re.split(r"\s{2,}", summary.strip(), maxsplit=1)
    return parts[1].strip() if len(parts) > 1 else summary.strip()


def _feature_visible(
    tool: ToolSpec, enabled_features: Collection[str] | None = None
) -> bool:
    """这个工具所属的功能现在开着吗？

    `enabled_features` 给了就按它（一轮对话用同一份快照，避免中途变）；
    没给就读进程内缓存（缓存没初始化 = 放行，兼容测试 / 脚本）。
    """
    owner = feature_catalog.feature_for_tool(tool.name)
    if owner is None:
        return True
    if enabled_features is not None:
        return owner in enabled_features
    cached = feature_flags.snapshot()
    return True if cached is None else owner in cached


def native_tools(
    mode: str | None = None,
    *,
    game_active: bool = False,
    cheat_active: bool = False,
    enabled_features: Collection[str] | None = None,
) -> list[dict[str, Any]]:
    """原生 function calling 的工具清单（v3.8 / D169）。

    只含**写了 schema 的**工具（其余仍走提示词协议 —— D14 的两条路并存）。
    """
    specs = tuple(
        tool
        for tool in TOOLS
        if (mode is None or mode in tool.modes)
        and (game_active or not tool.game_only)
        and (cheat_active or not tool.cheat_only)
        and _feature_visible(tool, enabled_features)
    )
    return [
        {
            "type": "function",
            "function": {
                "name": spec.name,
                "description": _summary_description(spec.summary),
                "parameters": NATIVE_SCHEMAS[spec.name],
            },
        }
        for spec in specs
        if spec.name in NATIVE_SCHEMAS
    ]


def prompt_lines(
    mode: str | None = None,
    *,
    game_active: bool = False,
    cheat_active: bool = False,
    enabled_features: Collection[str] | None = None,
) -> list[str]:
    """system prompt 里的"可用工具"清单 —— 由 manifest 生成，不手写。

    `mode` 给了就**只列该 mode 该看的工具**（T2.0-8）：工具多了会互相挤，
    正常聊天不该看见搜网、入戏时不该看见搜网（一搜就出戏）。
    不传 = 列全部（兼容老调用点）。
    """
    return [
        f"- {tool.summary}"
        for tool in TOOLS
        if (mode is None or mode in tool.modes)
        and (game_active or not tool.game_only)
        and (cheat_active or not tool.cheat_only)
        and _feature_visible(tool, enabled_features)
    ]


def tool_names() -> set[str]:
    return set(_BY_NAME)


def execute(
    session: Session,
    friend_id: str,
    call: dict[str, Any],
    *,
    allow_image_lookup: bool = True,
    allow_screen_lookup: bool = True,
    denied_reason: str | None = None,
) -> dict[str, Any]:
    """执行一个工具调用，**永不抛异常**（错误也当结果回灌给模型，LLM协议§5.1）。

    返回 `{id, name, args, ok, result|error, duration_ms}`，直接进 `messages.tools_used`（D31）。

    `allow_image_lookup=False`（chat 模型看不了图）→ `look_image` 直接回一句人话，
    别让它拿到一张自己看不了的图在那儿瞎猜。

    `allow_screen_lookup=False` → `look_screen` 回一句"这一轮已经看过了"（**一轮只看一次**，
    用户 2026-09-27 拍板：要有这个能力、但要控量，不要时间冷却）。
    """
    started = time.monotonic()
    name = str(call.get("name") or "")
    args = call.get("arguments") if isinstance(call.get("arguments"), dict) else {}
    record: dict[str, Any] = {
        "id": call.get("id"),
        "name": name,
        "args": args,
        "ok": False,
    }

    tool = _BY_NAME.get(name)
    if tool is None:
        record["error"] = f"没有这个工具：{name or '(空)'}"
        logger.warning("模型调了不存在的工具：{!r}", name)
    elif not feature_flags.tool_enabled(name):
        owner = feature_catalog.feature_for_tool(name) or ""
        feature = feature_catalog.get_feature(owner)
        label = feature.name if feature is not None else owner
        record["error"] = f"「{label}」这个功能现在是关着的 —— 如实告诉 TA，别假装做了"
        logger.info("工具 {} 属于已关闭的功能 {}，拒绝执行", name, owner)
    elif denied_reason:
        record["error"] = denied_reason
        logger.info("工具 {} 被用户拒绝，没有执行", name)
    elif name == "look_image" and not allow_image_lookup:
        record["error"] = "现在这个模型看不了图 —— 如实告诉 TA 你没法看图"
    elif name == "look_screen" and not allow_screen_lookup:
        record["error"] = "这一轮你已经看过屏幕了 —— 用刚才看到的那句话接着聊就行（一轮只看一次）"
    else:
        try:
            record["result"] = tool.handler(session, friend_id, args)
            record["ok"] = True
        except (ServiceError, ValueError) as exc:
            record["error"] = str(exc)
            logger.warning("工具 {} 执行失败（会回灌给模型）：{}", name, exc)
        except Exception as exc:  # noqa: BLE001 —— 工具炸了不能带崩整轮对话
            record["error"] = f"{type(exc).__name__}: {exc}"
            logger.exception("工具 {} 意外炸了", name)

    record["duration_ms"] = int((time.monotonic() - started) * 1000)
    return record


async def _run_tool(
    tool: ToolSpec | None,
    session: Session,
    friend_id: str,
    call: dict[str, Any],
    *,
    allow_image_lookup: bool = True,
    allow_screen_lookup: bool = True,
    denied_reason: str | None = None,
) -> dict[str, Any]:
    """执行工具，慢工具扔线程里（D136）。

    为什么能这么干：`execute()` **永不抛异常**（连参数错、工具炸了都归一成 error 结果），
    所以丢进线程不会把异常丢得到处都是；会话在 `await` 期间只有这一个线程在碰它
    （SQLite 那边 SQLAlchemy 默认已经 `check_same_thread=False`）。
    """
    if tool is not None and tool.blocking:
        return await asyncio.to_thread(
            execute,
            session,
            friend_id,
            call,
            allow_image_lookup=allow_image_lookup,
            allow_screen_lookup=allow_screen_lookup,
            denied_reason=denied_reason,
        )
    return execute(
        session,
        friend_id,
        call,
        allow_image_lookup=allow_image_lookup,
        allow_screen_lookup=allow_screen_lookup,
        denied_reason=denied_reason,
    )


async def _execute_async_unbounded(
    session: Session,
    friend_id: str,
    call: dict[str, Any],
    *,
    allow_image_lookup: bool = True,
    allow_screen_lookup: bool = True,
    friend_name: str = "朋友",
) -> dict[str, Any]:
    """异步版执行：**写操作先问用户**（v2.1 / D99）。

    读工具 / 普通工具还是走同步的 `execute`（很快）；只有 `mutating` 的工具才会
    先推一条审批事件、等用户点头（等在线程里，不堵事件循环）。
    标了 `blocking` 的工具（搜网 / 知识库 / 文件 IO）**扔到线程里跑**（D136）。
    """
    name = str(call.get("name") or "")
    args = call.get("arguments") if isinstance(call.get("arguments"), dict) else {}
    tool = _BY_NAME.get(name)
    if tool is not None and not feature_flags.tool_enabled(name):
        owner = feature_catalog.feature_for_tool(name) or ""
        feature = feature_catalog.get_feature(owner)
        label = feature.name if feature is not None else owner
        return {
            "id": call.get("id"),
            "name": name,
            "args": args,
            "ok": False,
            "error": f"「{label}」这个功能现在是关着的 —— 如实告诉 TA，别假装做了",
            "duration_ms": 0,
        }
    if tool is not None and tool.async_handler is not None and not tool.mutating:
        # D117：慢工具（画图）走异步 handler，别把事件循环堵住 7~30 秒
        started = time.monotonic()
        record: dict[str, Any] = {"id": call.get("id"), "name": name, "args": args, "ok": False}
        try:
            record["result"] = await tool.async_handler(session, friend_id, args)
            record["ok"] = True
        except (ServiceError, ValueError) as exc:
            record["error"] = str(exc)
            logger.warning("工具 {} 执行失败（会回灌给模型）：{}", name, exc)
        except Exception as exc:  # noqa: BLE001
            record["error"] = f"{type(exc).__name__}: {exc}"
            logger.exception("工具 {} 意外炸了", name)
        record["duration_ms"] = int((time.monotonic() - started) * 1000)
        return record
    if tool is None or not tool.mutating:
        return await _run_tool(
            tool,
            session,
            friend_id,
            call,
            allow_image_lookup=allow_image_lookup,
            allow_screen_lookup=allow_screen_lookup,
        )

    if not bool(settings_service.get(session).file_write_approval):
        logger.info("用户关了「文件改动要点头」→ 直接执行 {}", name)
        return await _run_tool(tool, session, friend_id, call, allow_image_lookup=allow_image_lookup)

    summary, detail = describe_mutation(name, args)
    allowed = await approval_service.ask(
        friend_id=friend_id,
        friend_name=friend_name,
        tool=name,
        summary=summary,
        detail=detail,
    )
    if not allowed:
        return await _run_tool(
            tool,
            session,
            friend_id,
            call,
            allow_image_lookup=allow_image_lookup,
            denied_reason="用户没有同意这次操作（TA 可能觉得不合适）—— 如实告诉 TA，别重试、别换个说法再试",
        )
    return await _run_tool(
        tool,
        session,
        friend_id,
        call,
        allow_image_lookup=allow_image_lookup,
    )


MAX_TOOL_RESULT_CHARS = 20000


def _cap_tool_result(record: dict[str, Any]) -> dict[str, Any]:
    """最后一道体积安全网：任何工具结果都不能把整段大 JSON 回灌给模型。"""
    if not record.get("ok") or "result" not in record:
        return record
    try:
        rendered = json.dumps(record["result"], ensure_ascii=False)
    except TypeError:
        return record
    if len(rendered) <= MAX_TOOL_RESULT_CHARS:
        return record
    record["result"] = {
        "truncated": True,
        "chars": len(rendered),
        "note": (
            "工具结果太大，已截断。缩小半径/数量，或改查具体坐标；"
            "不要原样重试。"
        ),
        "preview": rendered[:6000],
    }
    return record


async def execute_async(
    session: Session,
    friend_id: str,
    call: dict[str, Any],
    *,
    allow_image_lookup: bool = True,
    allow_screen_lookup: bool = True,
    friend_name: str = "朋友",
) -> dict[str, Any]:
    record = await _execute_async_unbounded(
        session,
        friend_id,
        call,
        allow_image_lookup=allow_image_lookup,
        allow_screen_lookup=allow_screen_lookup,
        friend_name=friend_name,
    )
    return _cap_tool_result(record)


def format_results(results: list[dict[str, Any]]) -> str:
    """把工具结果拼成回灌文本（LLM协议§4.4 的提示词协议格式）。"""
    lines = ["[工具执行结果]"]
    for item in results:
        payload: dict[str, Any] = {"ok": item["ok"]}
        if item["ok"]:
            result = item.get("result") or {}
            if isinstance(result, dict):
                # `_` 开头的内部字段（比如图的 data URL）不进回灌文本：图会另外附上
                result = {k: v for k, v in result.items() if not k.startswith("_")}
            payload.update(result)
        else:
            payload["error"] = item.get("error")
        lines.append(f"- {item['name']}: {json.dumps(payload, ensure_ascii=False)}")
    # ⚠️ 实测：不写这句，模型看到"执行成功"会**再调一遍**同一个工具
    # （add_reminder 因此建了两条重复提醒）。LLM协议§4.4 的格式上加一行即可。
    lines.append(
        "（以上都已经执行完了：不要重复调用同一个工具，也不要重复你上一条已经说过的话；"
        "有要补充的就说，没什么要补的短一句收尾即可。）"
    )
    return "\n".join(lines)
