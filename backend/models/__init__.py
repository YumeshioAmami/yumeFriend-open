"""ORM 模型（v1 五张 + v2 的 voices + v2.1 的知识库 + v3.16 的小纸条 + v3.45 留痕 + v3.46 向量 + v4.0 日程 + v5.0 游戏）。"""

from backend.models.blueprint import GameBlueprint, GameCreativeRecord
from backend.models.character_state import FriendState
from backend.models.document import Document, DocumentChunk
from backend.models.episode import FriendEpisode
from backend.models.expression import FriendExpression
from backend.models.friend import Friend
from backend.models.game import GameBinding, GameEvent, GameSession, GameTask
from backend.models.life import FriendLife, LifeActivity, LifeProject
from backend.models.memory import Memory
from backend.models.memory_embedding import MemoryEmbedding
from backend.models.memory_event import MemoryEvent
from backend.models.message import Message
from backend.models.schedule import Schedule, ScheduleEvent
from backend.models.settings import Settings
from backend.models.skill import GameSkill, GameSkillFailure
from backend.models.space import FriendSpace, FriendSpaceRuntime, SpaceEvent, SpaceObjectKnowledge
from backend.models.style import FriendStyleEvent, FriendStyleProfile
from backend.models.sync import (
    SyncBlob,
    SyncConflict,
    SyncDevice,
    SyncMessageOutbox,
    SyncMessageRelay,
    SyncMeta,
    SyncTombstone,
)
from backend.models.thought import Thought
from backend.models.voice import Voice

__all__ = [
    "Document",
    "DocumentChunk",
    "Friend",
    "FriendEpisode",
    "FriendExpression",
    "FriendLife",
    "GameBlueprint",
    "GameBinding",
    "GameCreativeRecord",
    "GameEvent",
    "GameSession",
    "GameSkill",
    "GameSkillFailure",
    "SyncBlob",
    "SyncConflict",
    "SyncDevice",
    "SyncMessageOutbox",
    "SyncMessageRelay",
    "SyncMeta",
    "SyncTombstone",
    "GameTask",
    "LifeActivity",
    "LifeProject",
    "FriendState",
    "FriendSpace",
    "FriendSpaceRuntime",
    "FriendStyleEvent",
    "FriendStyleProfile",
    "Message",
    "Memory",
    "MemoryEmbedding",
    "MemoryEvent",
    "Schedule",
    "ScheduleEvent",
    "Settings",
    "SpaceEvent",
    "SpaceObjectKnowledge",
    "Thought",
    "Voice",
]
