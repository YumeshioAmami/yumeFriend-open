"""蓝图库 / 自由创作记录（v5.1-P5 / D289）。"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from typing import Any, Mapping

from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.game.blueprints import Blueprint
from backend.models import GameBlueprint, GameCreativeRecord


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _new_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


def _dump(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False)


def _load(raw: str | None, default: Any) -> Any:
    try:
        value = json.loads(raw or "")
    except (json.JSONDecodeError, TypeError):
        return default
    return value if isinstance(value, type(default)) else default


def _hut_blueprint() -> dict[str, Any]:
    floor = [
        {"x": x, "y": 0, "z": z, "block": "cobblestone"}
        for x in range(3)
        for z in range(3)
    ]
    walls_1: list[dict[str, Any]] = []
    walls_2: list[dict[str, Any]] = []
    for y in (1, 2):
        target = walls_1 if y == 1 else walls_2
        for x in range(3):
            for z in range(3):
                if x == 0 or x == 2 or z == 0 or z == 2:
                    target.append({"x": x, "y": y, "z": z, "block": "oak_planks"})
    roof = [
        {"x": x, "y": 3, "z": z, "block": "oak_planks"}
        for x in range(3)
        for z in range(3)
    ]
    return {
        "name": "starter_hut",
        "description": "3×4×3 的橡木小屋：石地板、两圈木墙、木屋顶",
        "size": {"x": 3, "y": 4, "z": 3},
        "palette": {"floor": "cobblestone", "wall": "oak_planks"},
        "structure": "先铺石地板，再围两圈木墙，最后盖木屋顶；没有门洞，先做成封闭小屋",
        "layers": [
            {"y": 0, "blocks": floor},
            {"y": 1, "blocks": walls_1},
            {"y": 2, "blocks": walls_2},
            {"y": 3, "blocks": roof},
        ],
    }


BUILTIN_BLUEPRINTS = [_hut_blueprint()]


def ensure_builtin_blueprints(session: Session) -> None:
    now = _now()
    for raw in BUILTIN_BLUEPRINTS:
        blueprint = Blueprint.from_dict(raw)
        row = session.get(GameBlueprint, f"builtin:{blueprint.name}")
        data = _dump(raw)
        if row is None:
            session.add(
                GameBlueprint(
                    id=f"builtin:{blueprint.name}",
                    friend_id=None,
                    name=blueprint.name,
                    description=blueprint.description,
                    data=data,
                    source="builtin",
                    created_at=now,
                    updated_at=now,
                )
            )
        else:
            changed = (
                row.name != blueprint.name
                or row.description != blueprint.description
                or row.data != data
            )
            if changed:
                row.name = blueprint.name
                row.description = blueprint.description
                row.data = data
                row.source = "builtin"
                row.updated_at = now
    session.flush()


def _blueprint_row(row: GameBlueprint) -> dict[str, Any]:
    data = _load(row.data, {})
    if isinstance(data, dict) and "requirements" not in data:
        try:
            data["requirements"] = Blueprint.from_dict(data).requirements()
        except ValueError:
            data["requirements"] = {}
    return {
        "id": row.id,
        "friend_id": row.friend_id,
        "name": row.name,
        "description": row.description,
        "blueprint": data,
        "source": row.source,
        "created_at": row.created_at,
        "updated_at": row.updated_at,
    }


def list_blueprints(session: Session, friend_id: str) -> list[dict[str, Any]]:
    ensure_builtin_blueprints(session)
    rows = session.execute(
        select(GameBlueprint)
        .where(
            (GameBlueprint.friend_id.is_(None))
            | (GameBlueprint.friend_id == friend_id)
        )
        .order_by(GameBlueprint.source, GameBlueprint.name)
    ).scalars()
    return [_blueprint_row(row) for row in rows]


def get_blueprint(
    session: Session, friend_id: str, blueprint_id: str
) -> dict[str, Any] | None:
    ensure_builtin_blueprints(session)
    row = session.get(GameBlueprint, blueprint_id)
    if row is None:
        return None
    if row.friend_id not in (None, friend_id):
        raise PermissionError("这个蓝图不属于当前朋友")
    return _blueprint_row(row)


def save_blueprint(
    session: Session,
    *,
    friend_id: str,
    raw: Mapping[str, Any],
    source: str = "learned",
) -> dict[str, Any]:
    blueprint = Blueprint.from_dict(raw)
    now = _now()
    data = _dump(blueprint.view())
    existing = session.execute(
        select(GameBlueprint).where(
            GameBlueprint.friend_id == friend_id,
            GameBlueprint.name == blueprint.name,
        )
    ).scalar_one_or_none()
    if existing is not None:
        existing.description = blueprint.description
        existing.data = data
        existing.source = source
        existing.updated_at = now
        session.flush()
        return _blueprint_row(existing)
    row = GameBlueprint(
        id=_new_id("blueprint"),
        friend_id=friend_id,
        name=blueprint.name,
        description=blueprint.description,
        data=data,
        source=source,
        created_at=now,
        updated_at=now,
    )
    session.add(row)
    session.flush()
    return _blueprint_row(row)


def creative_start(
    session: Session,
    *,
    friend_id: str,
    brief: str,
    design: Mapping[str, Any],
    anchor: Mapping[str, Any],
    size: Mapping[str, Any],
    before: list[dict[str, Any]],
) -> dict[str, Any]:
    now = _now()
    row = GameCreativeRecord(
        id=_new_id("creative"),
        friend_id=friend_id,
        brief=brief,
        design=_dump(design),
        anchor=_dump(anchor),
        size=_dump(size),
        before=_dump(before),
        after="[]",
        diff="[]",
        rating=None,
        state="creating",
        created_at=now,
        updated_at=now,
    )
    session.add(row)
    session.flush()
    return _creative_row(row)


def creative_finish(
    session: Session,
    record_id: str,
    *,
    after: list[dict[str, Any]],
    diff: list[dict[str, Any]],
) -> dict[str, Any]:
    row = session.get(GameCreativeRecord, record_id)
    if row is None:
        raise ValueError(f"没有这个创作记录：{record_id}")
    row.after = _dump(after)
    row.diff = _dump(diff)
    row.state = "finished"
    row.updated_at = _now()
    session.flush()
    return _creative_row(row)


def creative_rate(
    session: Session, record_id: str, *, rating: int
) -> dict[str, Any]:
    row = session.get(GameCreativeRecord, record_id)
    if row is None:
        raise ValueError(f"没有这个创作记录：{record_id}")
    row.rating = max(1, min(5, int(rating)))
    row.updated_at = _now()
    session.flush()
    return _creative_row(row)


def get_creative(session: Session, record_id: str) -> dict[str, Any] | None:
    row = session.get(GameCreativeRecord, record_id)
    return _creative_row(row) if row is not None else None


def list_creative(
    session: Session, friend_id: str, *, limit: int = 30
) -> list[dict[str, Any]]:
    rows = session.execute(
        select(GameCreativeRecord)
        .where(GameCreativeRecord.friend_id == friend_id)
        .order_by(GameCreativeRecord.created_at.desc())
        .limit(limit)
    ).scalars()
    return [_creative_row(row) for row in rows]


def _creative_row(row: GameCreativeRecord) -> dict[str, Any]:
    return {
        "id": row.id,
        "friend_id": row.friend_id,
        "brief": row.brief,
        "design": _load(row.design, {}),
        "anchor": _load(row.anchor, {}),
        "size": _load(row.size, {}),
        "before": _load(row.before, []),
        "after": _load(row.after, []),
        "diff": _load(row.diff, []),
        "rating": row.rating,
        "state": row.state,
        "created_at": row.created_at,
        "updated_at": row.updated_at,
    }
