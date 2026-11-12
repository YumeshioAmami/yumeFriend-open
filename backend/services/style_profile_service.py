"""说话方式档案（D347）。

设计边界：
  · 普通记忆（memories）记事实；这里只记「她该怎么说话」。
  · 人格（friends.personality）和声音样本（friends.voice_examples）永远不改。
  · 原始反馈留事件日志；真正注入 prompt 的是压缩后的当前档案。
"""

from __future__ import annotations

import copy
import difflib
import json
import re
import secrets
import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.models import Friend, FriendStyleEvent, FriendStyleProfile, Message

SECTION_ORDER = ("length", "tone", "diction", "structure", "situations", "keep")
SECTION_LABELS = {
    "length": "长度",
    "tone": "语气",
    "diction": "用词和口癖",
    "structure": "解释和结构",
    "situations": "不同场景",
    "keep": "做得好的地方",
}
SECTION_LIMITS = {
    "length": 3,
    "tone": 4,
    "diction": 4,
    "structure": 3,
    "situations": 4,
    "keep": 3,
}
MAX_LESSON_CHARS = 60
MAX_CHANGES_PER_CALL = 6
SIMILARITY_MERGE = 0.82

_PUNCT_RE = re.compile(r"[\s，。！？!?、；;：:\"'“”‘’（）()\[\]【】~～…\-—]+")
_STYLE_TERM_RE = re.compile(
    r"(太长|太短|啰嗦|小作文|写报告|太满|太正式|太书面|像\s*ai|ai\s*味|不像你|"
    r"说话方式(?:有点|太|不|改|像|自然|别|怪|问题)|"
    r"表达方式(?:有点|太|不|改|像|自然|别|怪|问题)|"
    r"语气(?:不对|太|有点|自然|冷|硬|软|冲|怪)|"
    r"用词(?:不对|太|有点|怪|生硬)|口癖|"
    r"别(?:再|老|总|一直)?[说用]|别总结|别列|短一点|少说一点|直接说)",
    re.IGNORECASE,
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def empty_profile(version: int = 0) -> dict[str, Any]:
    return {
        "version": int(version),
        "sections": {key: [] for key in SECTION_ORDER},
    }


def _normalized(text: str) -> str:
    return _PUNCT_RE.sub("", str(text or "").lower())


def looks_like_explicit_feedback(text: str) -> bool:
    """保守识别“TA 在评价她的表达方式”。

    这不是自动学习入口 —— 真正写不写仍由模型调用 `reflect_speech` 决定。
    它只负责在**明确反馈的那一轮**贴一条近生成点提醒，防止她只口头答应。
    """
    raw = str(text or "").strip()
    if not raw or not _STYLE_TERM_RE.search(raw):
        return False
    if any(
        word in raw
        for word in ("你", "妳", "说话", "表达", "语气", "用词", "口癖")
    ):
        return True
    # 没有“你”时，只认很强的元表达，避免把“这个视频太长”误判成反馈。
    return any(
        phrase in raw
        for phrase in (
            "说话方式",
            "表达方式",
            "像AI",
            "像 AI",
            "AI味",
            "小作文",
            "写报告",
            "别总结",
            "别列",
            "直接说",
            "短一点",
            "语气自然",
            "别老用",
            "别总用",
            "用词",
        )
    )


def fallback_changes(text: str) -> list[dict[str, str]]:
    """模型漏调 reflect_speech 时的保守兜底：只从明确反馈里压一条规则。"""
    raw = str(text or "").strip()
    if not looks_like_explicit_feedback(raw):
        return []
    rules: list[tuple[str, str]] = []
    if any(word in raw for word in ("太长", "太满", "啰嗦", "小作文", "写报告", "展开")):
        rules.append(
            ("length", "日常回复先短一点，先接具体事；不要写成小作文或报告")
        )
    if any(word in raw for word in ("别总结", "别列", "不要总结", "不要列", "直接说", "别分析")):
        rules.append(
            ("structure", "先直接回应，不要总结、列点或做多余分析")
        )
    if any(word in raw for word in ("别老用", "别总用", "别再用", "口癖", "用词", "本鲸鱼")):
        rules.append(
            ("diction", "少用固定口癖和段子，按当下语境换自然说法")
        )
    if any(
        word in raw
        for word in ("语气", "像AI", "像 AI", "AI味", "不像你", "太冷", "太硬", "太正式", "自然点")
    ):
        rules.append(
            ("tone", "语气保持自然、像朋友聊天，不要客服腔或报告腔")
        )
    if any(word in raw for word in ("这样很好", "就这样", "保持", "喜欢这样")):
        rules.append(("keep", "保持现在这种自然的表达方式"))
    if not rules:
        rules.append(("tone", "按当下语境自然说话，不要用客服腔或报告腔"))
    seen: set[str] = set()
    out: list[dict[str, str]] = []
    for section, lesson in rules:
        if section in seen:
            continue
        seen.add(section)
        out.append(
            {
                "action": "add",
                "section": section,
                "lesson": lesson,
                "why": "TA 明确反馈了表达方式，模型没有调用工具时的兜底",
            }
        )
    return out[:2]


def _similarity(left: str, right: str) -> float:
    a, b = _normalized(left), _normalized(right)
    if not a or not b:
        return 0.0
    return difflib.SequenceMatcher(None, a, b).ratio()


def _clean_lesson(value: Any) -> str:
    text = " ".join(str(value or "").split()).strip()
    if not text:
        raise ValueError("lesson 不能为空")
    if len(text) > MAX_LESSON_CHARS:
        raise ValueError(
            f"lesson 太长了（{len(text)} 字）；请压成不超过 {MAX_LESSON_CHARS} 字的一句可执行规则"
        )
    return text


def _clean_change(raw: Any) -> dict[str, str]:
    if not isinstance(raw, dict):
        raise ValueError("changes 里的每一项都必须是对象")
    action = str(raw.get("action") or "").strip().lower()
    if action not in ("add", "update", "remove"):
        raise ValueError("action 只能是 add / update / remove")
    section = str(raw.get("section") or "").strip().lower()
    if section not in SECTION_ORDER:
        raise ValueError(f"section 只能是：{', '.join(SECTION_ORDER)}")
    change = {
        "action": action,
        "section": section,
        "target_id": str(raw.get("target_id") or "").strip(),
        "why": " ".join(str(raw.get("why") or "").split()).strip()[:120],
    }
    if action in ("add", "update"):
        change["lesson"] = _clean_lesson(raw.get("lesson"))
    if action in ("update", "remove") and not change["target_id"]:
        raise ValueError(f"{action} 必须带 target_id（档案里每条前面的 [id]）")
    return change


def _load_profile(raw: str | None, version_hint: int = 0) -> dict[str, Any]:
    try:
        parsed = json.loads(raw or "")
    except (TypeError, ValueError):
        parsed = {}
    if not isinstance(parsed, dict):
        parsed = {}
    sections_raw = parsed.get("sections")
    sections: dict[str, list[dict[str, Any]]] = {}
    for key in SECTION_ORDER:
        rows = sections_raw.get(key) if isinstance(sections_raw, dict) else []
        cleaned: list[dict[str, Any]] = []
        for row in rows if isinstance(rows, list) else []:
            if not isinstance(row, dict):
                continue
            text = str(row.get("text") or "").strip()
            line_id = str(row.get("id") or "").strip()
            if not text or not line_id:
                continue
            cleaned.append(
                {
                    "id": line_id,
                    "text": text[:MAX_LESSON_CHARS],
                    "hits": max(1, int(row.get("hits") or 1)),
                    "created_at": str(row.get("created_at") or ""),
                    "updated_at": str(row.get("updated_at") or ""),
                }
            )
        sections[key] = cleaned[: SECTION_LIMITS[key]]
    version = parsed.get("version")
    try:
        version = max(0, int(version))
    except (TypeError, ValueError):
        version = max(0, int(version_hint))
    return {"version": version, "sections": sections}


def _dump(profile: dict[str, Any]) -> str:
    return json.dumps(profile, ensure_ascii=False, separators=(",", ":"))


def _find_line(profile: dict[str, Any], section: str, line_id: str) -> dict[str, Any] | None:
    for line in profile["sections"][section]:
        if line["id"] == line_id:
            return line
    return None


def _section_lines_text(profile: dict[str, Any], section: str) -> str:
    lines = profile["sections"][section]
    if not lines:
        return "（空）"
    return "；".join(f"[{line['id']}] {line['text']}" for line in lines)


def _apply_one(profile: dict[str, Any], raw: Any, now: str) -> dict[str, Any]:
    change = _clean_change(raw)
    section = change["section"]
    lines = profile["sections"][section]
    action = change["action"]

    if action == "add":
        lesson = change["lesson"]
        similar = next(
            (
                line
                for line in lines
                if _similarity(line["text"], lesson) >= SIMILARITY_MERGE
            ),
            None,
        )
        if similar is not None:
            similar["text"] = lesson
            similar["hits"] += 1
            similar["updated_at"] = now
            return {
                "action": "reinforced",
                "section": section,
                "line_id": similar["id"],
                "lesson": similar["text"],
                "hits": similar["hits"],
            }
        if len(lines) >= SECTION_LIMITS[section]:
            raise ValueError(
                f"「{SECTION_LABELS[section]}」已经有 {SECTION_LIMITS[section]} 条，满了；"
                f"先 update 或 remove 其中一条。当前：{_section_lines_text(profile, section)}"
            )
        line = {
            "id": secrets.token_hex(4),
            "text": lesson,
            "hits": 1,
            "created_at": now,
            "updated_at": now,
        }
        lines.append(line)
        return {
            "action": "added",
            "section": section,
            "line_id": line["id"],
            "lesson": lesson,
            "hits": 1,
        }

    target = _find_line(profile, section, change["target_id"])
    if target is None:
        raise ValueError(
            f"找不到 target_id={change['target_id']!r}；"
            f"当前「{SECTION_LABELS[section]}」是：{_section_lines_text(profile, section)}"
        )
    if action == "update":
        target["text"] = change["lesson"]
        target["hits"] += 1
        target["updated_at"] = now
        return {
            "action": "updated",
            "section": section,
            "line_id": target["id"],
            "lesson": target["text"],
            "hits": target["hits"],
        }

    lines.remove(target)
    return {
        "action": "removed",
        "section": section,
        "line_id": target["id"],
        "lesson": target["text"],
    }


def _get_or_create_row(session: Session, friend_id: str) -> FriendStyleProfile:
    friend = session.get(Friend, friend_id)
    if friend is None:
        raise ValueError("朋友不存在")
    row = session.get(FriendStyleProfile, friend_id)
    if row is None:
        row = FriendStyleProfile(
            friend_id=friend_id,
            profile_json=_dump(empty_profile()),
            version=0,
            updated_at=_now(),
        )
        session.add(row)
        session.flush()
    return row


def get_profile(session: Session, friend_id: str) -> dict[str, Any]:
    row = session.get(FriendStyleProfile, friend_id)
    if row is None:
        return empty_profile()
    return _load_profile(row.profile_json, row.version)


def prompt_note(session: Session, friend_id: str) -> str:
    profile = get_profile(session, friend_id)
    blocks: list[str] = []
    for key in SECTION_ORDER:
        lines = profile["sections"][key]
        if not lines:
            continue
        blocks.append(f"## {SECTION_LABELS[key]}")
        blocks.extend(f"- [{line['id']}] {line['text']}" for line in lines)
    return "\n".join(blocks)


def _latest_feedback_context(session: Session, friend_id: str) -> tuple[str | None, str | None]:
    rows = list(
        session.execute(
            select(Message)
            .where(Message.friend_id == friend_id)
            .order_by(Message.created_at.desc())
            .limit(8)
        ).scalars()
    )
    user_text = next((row.content for row in rows if row.role == "user"), None)
    assistant_excerpt = next((row.content for row in rows if row.role == "assistant"), None)
    return user_text, assistant_excerpt


def apply_changes(
    session: Session,
    friend_id: str,
    changes: Any,
    *,
    summary: str | None = None,
    user_text: str | None = None,
    assistant_excerpt: str | None = None,
    reason: str | None = None,
    event_type: str = "reflect",
) -> dict[str, Any]:
    if not isinstance(changes, list) or not changes:
        raise ValueError("changes 至少要有一条")
    if len(changes) > MAX_CHANGES_PER_CALL:
        raise ValueError(f"一次最多改 {MAX_CHANGES_PER_CALL} 条")

    row = _get_or_create_row(session, friend_id)
    before = _load_profile(row.profile_json, row.version)
    before_version = int(row.version)
    working = copy.deepcopy(before)
    now = _now()
    applied = [_apply_one(working, item, now) for item in changes]
    after_version = before_version + 1
    working["version"] = after_version

    auto_user_text, auto_assistant_excerpt = _latest_feedback_context(session, friend_id)
    user_text = (user_text or auto_user_text or "").strip()[:2000] or None
    assistant_excerpt = (assistant_excerpt or auto_assistant_excerpt or "").strip()[:2000] or None

    row.profile_json = _dump(working)
    row.version = after_version
    row.updated_at = now
    event = FriendStyleEvent(
        id=uuid.uuid4().hex,
        friend_id=friend_id,
        event_type=event_type,
        summary=(summary or "").strip()[:400] or None,
        changes_json=json.dumps(applied, ensure_ascii=False),
        user_text=user_text,
        assistant_excerpt=assistant_excerpt,
        reason=(reason or "").strip()[:400] or None,
        before_version=before_version,
        after_version=after_version,
        before_profile_json=_dump(before),
        after_profile_json=_dump(working),
        created_at=now,
    )
    session.add(event)
    session.flush()
    return {
        "version": after_version,
        "applied": applied,
        "profile": working,
        "event_id": event.id,
    }


def reset_profile(session: Session, friend_id: str, *, reason: str | None = None) -> dict[str, Any]:
    row = _get_or_create_row(session, friend_id)
    before = _load_profile(row.profile_json, row.version)
    after_version = int(row.version) + 1
    after = empty_profile(after_version)
    now = _now()
    row.profile_json = _dump(after)
    row.version = after_version
    row.updated_at = now
    event = FriendStyleEvent(
        id=uuid.uuid4().hex,
        friend_id=friend_id,
        event_type="reset",
        summary="清空说话方式档案",
        changes_json=None,
        reason=(reason or "").strip()[:400] or None,
        before_version=int(before["version"]),
        after_version=after_version,
        before_profile_json=_dump(before),
        after_profile_json=_dump(after),
        created_at=now,
    )
    session.add(event)
    session.flush()
    return {"version": after_version, "event_id": event.id}


def rollback_event(session: Session, friend_id: str, event_id: str) -> dict[str, Any]:
    source = session.get(FriendStyleEvent, event_id)
    if source is None or source.friend_id != friend_id:
        raise ValueError("找不到这条学习记录")
    row = _get_or_create_row(session, friend_id)
    before = _load_profile(row.profile_json, row.version)
    restored = _load_profile(source.before_profile_json)
    after_version = int(row.version) + 1
    restored["version"] = after_version
    now = _now()
    row.profile_json = _dump(restored)
    row.version = after_version
    row.updated_at = now
    event = FriendStyleEvent(
        id=uuid.uuid4().hex,
        friend_id=friend_id,
        event_type="rollback",
        summary=f"撤销学习事件 {event_id}",
        changes_json=None,
        reason=f"rollback:{event_id}",
        before_version=int(before["version"]),
        after_version=after_version,
        before_profile_json=_dump(before),
        after_profile_json=_dump(restored),
        created_at=now,
    )
    session.add(event)
    session.flush()
    return {"version": after_version, "event_id": event.id, "profile": restored}


def list_events(session: Session, friend_id: str, *, limit: int = 50) -> list[FriendStyleEvent]:
    return list(
        session.execute(
            select(FriendStyleEvent)
            .where(FriendStyleEvent.friend_id == friend_id)
            .order_by(FriendStyleEvent.created_at.desc())
            .limit(max(1, min(200, int(limit))))
        ).scalars()
    )
