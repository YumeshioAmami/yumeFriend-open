"""技能书持久化与检索（v5.1-P4 / D288）。"""

from __future__ import annotations

import json
import math
import re
import uuid
from datetime import datetime, timezone
from typing import Any, Mapping

from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.game import skills as skill_runtime
from backend.models import GameSkill, GameSkillFailure


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _new_id(prefix: str = "skill") -> str:
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


def _dump(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False)


def _load(raw: str | None, default: Any) -> Any:
    try:
        value = json.loads(raw or "")
    except (json.JSONDecodeError, TypeError):
        return default
    return value if isinstance(value, type(default)) else default


def _terms(text: str) -> set[str]:
    lowered = (text or "").lower()
    words = set(re.findall(r"[a-z0-9_]+", lowered))
    cjk = re.findall(r"[\u3400-\u9fff]+", lowered)
    for chunk in cjk:
        for size in (2, 3):
            words.update(chunk[index : index + size] for index in range(len(chunk) - size + 1))
    return {word for word in words if word}


def _vector_score(query: str, texts: list[str]) -> list[float] | None:
    """模型已加载时叠一层语义分；没加载就完全退回关键词（不触发下载）。"""
    try:
        from backend.services import embedding_service  # noqa: PLC0415

        if not embedding_service.ready() or not texts:
            return None
        qvecs = embedding_service.embed_texts([query], query=True)
        tvecs = embedding_service.embed_texts(texts)
        if not qvecs or not tvecs or len(qvecs) != 1 or len(tvecs) != len(texts):
            return None
        q = qvecs[0]
        qnorm = math.sqrt(sum(value * value for value in q)) or 1.0
        scores = []
        for vector in tvecs:
            norm = math.sqrt(sum(value * value for value in vector)) or 1.0
            scores.append(
                sum(a * b for a, b in zip(q, vector)) / (qnorm * norm)
            )
        return scores
    except Exception:  # noqa: BLE001 —— 检索退化不能拖垮游戏
        return None


def _row_to_dict(row: GameSkill) -> dict[str, Any]:
    return {
        "id": row.id,
        "friend_id": row.friend_id,
        "name": row.name,
        "description": row.description,
        "params": _load(row.params, {}),
        "preconditions": _load(row.preconditions, []),
        "steps": _load(row.steps, []),
        "postconditions": _load(row.postconditions, []),
        "versions": _load(row.versions, {}),
        "mods": _load(row.mods, []),
        "stats": _load(row.stats, {}),
        "enabled": bool(row.enabled),
        "source": row.source,
        "created_at": row.created_at,
        "updated_at": row.updated_at,
    }


def ensure_builtin_skills(session: Session) -> None:
    """把内置技能同步进库；只更新定义，不动用户/朋友设的 enabled。"""
    now = _now()
    for definition in skill_runtime.BUILTIN_SKILLS:
        name = str(definition["name"])
        skill_id = f"builtin:{name}"
        row = session.get(GameSkill, skill_id)
        values = {
            "name": name,
            "description": str(definition.get("description") or ""),
            "params": _dump(definition.get("params") or {}),
            "preconditions": _dump(definition.get("preconditions") or []),
            "steps": _dump(definition.get("steps") or []),
            "postconditions": _dump(definition.get("postconditions") or []),
            "versions": _dump(definition.get("versions") or {}),
            "mods": _dump(definition.get("mods") or []),
            "source": "builtin",
        }
        if row is None:
            session.add(
                GameSkill(
                    id=skill_id,
                    friend_id=None,
                    enabled=1,
                    stats=_dump(definition.get("stats") or {}),
                    created_at=now,
                    updated_at=now,
                    **values,
                )
            )
        else:
            changed = False
            for key, value in values.items():
                if getattr(row, key) != value:
                    setattr(row, key, value)
                    changed = True
            if changed:
                row.updated_at = now
    session.flush()


def list_skills(
    session: Session, friend_id: str, *, include_disabled: bool = True
) -> list[dict[str, Any]]:
    ensure_builtin_skills(session)
    query = select(GameSkill).where(
        (GameSkill.friend_id.is_(None)) | (GameSkill.friend_id == friend_id)
    )
    if not include_disabled:
        query = query.where(GameSkill.enabled == 1)
    rows = session.execute(query.order_by(GameSkill.source, GameSkill.name)).scalars()
    return [_row_to_dict(row) for row in rows]


def get_skill(session: Session, friend_id: str, name: str) -> dict[str, Any] | None:
    ensure_builtin_skills(session)
    rows = list(
        session.execute(
            select(GameSkill).where(
                GameSkill.name == name,
                (GameSkill.friend_id.is_(None)) | (GameSkill.friend_id == friend_id),
            )
        ).scalars()
    )
    if not rows:
        return None
    rows.sort(key=lambda row: row.friend_id is None)  # 朋友自己的优先
    return _row_to_dict(rows[0])


def get_skill_by_id(session: Session, skill_id: str) -> dict[str, Any] | None:
    ensure_builtin_skills(session)
    row = session.get(GameSkill, skill_id)
    return _row_to_dict(row) if row is not None else None


def set_enabled(
    session: Session, skill_id: str, friend_id: str, enabled: bool
) -> dict[str, Any]:
    row = session.get(GameSkill, skill_id)
    if row is None:
        raise ValueError(f"没有这个技能：{skill_id}")
    if row.friend_id not in (None, friend_id):
        raise PermissionError("这个技能不属于当前朋友")
    row.enabled = 1 if enabled else 0
    row.updated_at = _now()
    session.flush()
    return _row_to_dict(row)


def delete_skill(session: Session, skill_id: str, friend_id: str) -> None:
    row = session.get(GameSkill, skill_id)
    if row is None:
        return
    if row.friend_id != friend_id:
        raise PermissionError("内置技能不能删除，只能禁用；学会的技能才可删除")
    session.delete(row)
    session.flush()


def learn_from_plan(
    session: Session,
    *,
    friend_id: str,
    plan: Any,
    game_kind: str,
) -> dict[str, Any] | None:
    """只接受“所有 task 成功 + plan checks 全过”的 plan。"""
    if not plan.checks:
        return None
    result = plan.result if isinstance(plan.result, dict) else {}
    if result.get("verified") is not True:
        return None
    if any(task.status != "success" for task in plan.tasks):
        return None
    steps = [
        {
            "action": task.action,
            "args": dict(task.args),
            "checks": list(task.checks),
        }
        for task in plan.tasks
    ]
    signature = _dump(
        {"description": plan.expected, "steps": steps, "checks": plan.checks}
    )
    for row in list_skills(session, friend_id):
        if row["source"] != "learned" or row["friend_id"] != friend_id:
            continue
        existing = _dump(
            {
                "description": row["description"],
                "steps": row["steps"],
                "checks": row["postconditions"],
            }
        )
        if existing == signature:
            stats = dict(row["stats"])
            stats["learned_from"] = plan.id
            stats["reuses"] = int(stats.get("reuses") or 0) + 1
            db_row = session.get(GameSkill, row["id"])
            if db_row is not None:
                db_row.stats = _dump(stats)
                db_row.updated_at = _now()
                session.flush()
                return _row_to_dict(db_row)

    now = _now()
    row = GameSkill(
        id=_new_id("learned"),
        friend_id=friend_id,
        name=_new_id("plan"),
        description=str(plan.expected or ""),
        params="{}",
        preconditions=_dump(
            [plan.condition] if plan.condition else []
        ),
        steps=_dump(steps),
        postconditions=_dump(list(plan.checks)),
        versions=_dump({"game_kind": game_kind}),
        mods="[]",
        stats=_dump(
            {
                "runs": 0,
                "successes": 0,
                "failures": 0,
                "learned_from": plan.id,
                "reuses": 0,
            }
        ),
        enabled=1,
        source="learned",
        created_at=now,
        updated_at=now,
    )
    session.add(row)
    session.flush()
    return _row_to_dict(row)


def record_run(session: Session, skill_id: str, *, ok: bool) -> None:
    row = session.get(GameSkill, skill_id)
    if row is None:
        return
    stats = _load(row.stats, {})
    stats["runs"] = int(stats.get("runs") or 0) + 1
    key = "successes" if ok else "failures"
    stats[key] = int(stats.get(key) or 0) + 1
    stats["last_run_at"] = _now()
    row.stats = _dump(stats)
    row.updated_at = _now()
    session.flush()


def record_failure(
    session: Session,
    *,
    friend_id: str,
    plan_id: str | None,
    reason: str,
    detail: str,
    skill_name: str | None = None,
) -> None:
    detail = " ".join(str(detail or "").split())[:1000]
    existing = session.execute(
        select(GameSkillFailure).where(
            GameSkillFailure.friend_id == friend_id,
            GameSkillFailure.plan_id == plan_id,
            GameSkillFailure.reason == reason,
            GameSkillFailure.detail == detail,
        )
    ).scalar_one_or_none()
    if existing is not None:
        return
    session.add(
        GameSkillFailure(
            id=_new_id("failure"),
            friend_id=friend_id,
            plan_id=plan_id,
            skill_name=skill_name,
            reason=reason,
            detail=detail,
            created_at=_now(),
        )
    )
    session.flush()


def list_failures(
    session: Session, friend_id: str, *, limit: int = 30
) -> list[dict[str, Any]]:
    rows = session.execute(
        select(GameSkillFailure)
        .where(GameSkillFailure.friend_id == friend_id)
        .order_by(GameSkillFailure.created_at.desc())
        .limit(limit)
    ).scalars()
    return [
        {
            "id": row.id,
            "friend_id": row.friend_id,
            "plan_id": row.plan_id,
            "skill_name": row.skill_name,
            "reason": row.reason,
            "detail": row.detail,
            "created_at": row.created_at,
        }
        for row in rows
    ]


def search_skills(
    session: Session,
    friend_id: str,
    query: str,
    *,
    game_kind: str | None = None,
    limit: int = 5,
) -> list[dict[str, Any]]:
    """关键词优先，模型已就绪时再叠语义分；最多 5 条。"""
    candidates = []
    for row in list_skills(session, friend_id, include_disabled=False):
        version = row.get("versions") if isinstance(row.get("versions"), dict) else {}
        if game_kind and version.get("game_kind") not in (None, game_kind):
            continue
        candidates.append(row)
    if not candidates:
        return []
    qterms = _terms(query)
    texts = [
        f"{row['name']} {row['description']} " + _dump(row.get("steps") or [])
        for row in candidates
    ]
    vectors = _vector_score(query, texts) if qterms else None
    scored: list[tuple[float, dict[str, Any]]] = []
    lowered_query = (query or "").lower().strip()
    for index, (row, text) in enumerate(zip(candidates, texts)):
        terms = _terms(text)
        overlap = len(qterms & terms)
        score = float(overlap)
        if lowered_query and lowered_query in text.lower():
            score += 3.0
        if vectors and index < len(vectors):
            score += max(0.0, vectors[index]) * 2.0
        scored.append((score, row))
    scored.sort(key=lambda item: (-item[0], item[1]["source"] != "builtin", item[1]["name"]))
    top = max(1, min(5, int(limit)))
    return [row for _score, row in scored[:top]]
