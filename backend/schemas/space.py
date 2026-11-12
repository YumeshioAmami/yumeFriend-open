"""小世界请求体（v7.21 / D374）。"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class SpaceStatePatch(BaseModel):
    layout: dict[str, list[float]] | None = None
    mode: Literal["free", "together"] | None = None
    current_action: dict | None = None
    prop_states: dict[str, str] | None = None
    world: dict | None = None
    appearance: dict | None = None


class SpaceEventIn(BaseModel):
    kind: str = Field(min_length=1, max_length=20)
    target: str | None = Field(default=None, max_length=40)
    detail: dict | None = None
    notable: bool | None = None


class SpaceViewerIn(BaseModel):
    """Phase A：用户在小世界里的相机/视线/手。只用于当前这一次思考，不落库。"""

    camera: tuple[float, float, float] | None = None
    look: tuple[float, float, float] | None = None
    hand: tuple[float, float, float] | None = None
    hand_active: bool = False
    hand_holding: str | None = Field(default=None, max_length=40)
    hand_contact_zone: str | None = Field(default=None, max_length=20)
    hand_contact_kind: str | None = Field(default=None, max_length=20)
    hand_contact_speed: float = Field(default=0.0, ge=0.0, le=50.0)
    hand_contact_seconds: float = Field(default=0.0, ge=0.0, le=3600.0)
    last_moved_at: float | None = None


class SpaceThinkIn(BaseModel):
    text: str | None = Field(default=None, max_length=1000)
    viewer: SpaceViewerIn | None = None


class SpaceSelfStateIn(BaseModel):
    """她自己的身体状态；由前端物理角色控制器提供。"""

    position: tuple[float, float, float] | None = None
    hand: tuple[float, float, float] | None = None
    holding: str | None = Field(default=None, max_length=80)
    posture: str = Field(default="stand", max_length=10)
    sitting: bool = False
    lying: bool = False
    support_key: str | None = Field(default=None, max_length=80)
    moving: bool = False
    still_seconds: float = Field(default=0.0, ge=0.0, le=600.0)
    target: str | None = Field(default=None, max_length=80)
    at_target: bool = False


class SpacePerceptionObjectIn(BaseModel):
    key: str = Field(min_length=1, max_length=80)
    label: str = Field(default="", max_length=40)
    position: tuple[float, float, float]
    moving: bool = False


class SpacePerceptionIn(BaseModel):
    viewer: SpaceViewerIn | None = None
    self_state: SpaceSelfStateIn | None = None
    objects: list[SpacePerceptionObjectIn] = Field(default_factory=list, max_length=40)
    paused: bool = False
    personality_curiosity: float = Field(default=0.5, ge=0.0, le=1.0)
    activity_kind: str | None = Field(default=None, max_length=40)
    activity_target: str | None = Field(default=None, max_length=80)
    activity_label: str | None = Field(default=None, max_length=120)
    mood: str | None = Field(default=None, max_length=40)


class SpaceAskIn(BaseModel):
    object_key: str = Field(min_length=1, max_length=80)
    viewer: SpaceViewerIn | None = None
    activity_kind: str | None = Field(default=None, max_length=40)
    activity_target: str | None = Field(default=None, max_length=80)
    activity_label: str | None = Field(default=None, max_length=120)
    mood: str | None = Field(default=None, max_length=40)


class SpaceReactionIn(BaseModel):
    zone: str = Field(min_length=1, max_length=20)
    viewer: SpaceViewerIn | None = None
    self_state: SpaceSelfStateIn | None = None
    objects: list[SpacePerceptionObjectIn] = Field(default_factory=list, max_length=40)
    activity_label: str | None = Field(default=None, max_length=120)
    mood: str | None = Field(default=None, max_length=40)
    interaction: str | None = Field(default=None, max_length=20)
    sustained_seconds: float = Field(default=0.0, ge=0.0, le=3600.0)


class SpaceActionResultIn(BaseModel):
    action_id: str = Field(min_length=1, max_length=80)
    ok: bool = True
    detail: str = Field(default="", max_length=500)
