"""功能目录（v7.20 / D373）—— 全项目"哪个功能拥有什么"的唯一清单。

加一个新功能时，先在这里加一条 `FeatureSpec`，再去写代码。
`tests/test_features.py` 会做机械校验：

- 每个工具都必须属于一个存在的功能；
- 每个功能声明的工具都必须真的存在；
- prompt 块 / 后台任务 / 数据表的归属必须唯一；
- 新工具忘了写归属会直接测试失败，逼着人回来登记。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

CORE_FEATURE_ID = "core"


@dataclass(frozen=True)
class FeatureSpec:
    """一个功能模块的静态说明。

    注意：这里只描述"拥有什么"，不描述"怎么实现"。实现仍在原来的
    api / services / agent 里；本层负责**开关、门控、清单和事件**。
    """

    id: str
    name: str
    description: str
    category: str = "其他"
    default_enabled: bool = True
    core: bool = False
    tools: tuple[str, ...] = ()
    prompt_blocks: tuple[str, ...] = ()
    jobs: tuple[str, ...] = ()
    tables: tuple[str, ...] = ()
    # 用正则描述这个功能占的 HTTP 路径；中间件据此拒绝已关闭功能的请求。
    # 没有匹配到的路径一律放行（宁可漏拦，也不要误拦核心聊天）。
    api_patterns: tuple[str, ...] = ()
    ui_entries: tuple[str, ...] = ()


FEATURES: tuple[FeatureSpec, ...] = (
    FeatureSpec(
        id=CORE_FEATURE_ID,
        name="核心聊天",
        description="朋友人格、mode、聊天回合、记忆、时间与角色状态。关不掉。",
        category="核心",
        core=True,
        tools=(
            "save_memory",
            "update_memory",
            "delete_memory",
            "recall_memory",
            "get_time",
            "switch_mode",
            "switch_role",
            "adjust_rp_playstyle",
        ),
        prompt_blocks=(
            "memory",
            "appearance",
            "voice",
            "episode",
            "pacing",
            "state",
            "mode",
            "tool_block",
        ),
        jobs=(
            "memory_catchup",
            "episode_reflection",
            "memory_cleanup",
            "proactive_heartbeat",
            "llm_warmup",
            "memory_embedding_backfill",
        ),
        tables=(
            "friends",
            "messages",
            "memories",
            "memory_events",
            "memory_embeddings",
            "friend_states",
            "friend_episodes",
            "settings",
            "voices",
            "sync_meta",
            "sync_devices",
            "sync_tombstones",
            "sync_conflicts",
            "sync_blobs",
        ),
    ),
    FeatureSpec(
        id="thought",
        name="小纸条",
        description="她答应过的事、在意的事、想说又咽回去的话，会记成有寿命的小纸条。",
        category="聊天能力",
        tools=("save_thought",),
        prompt_blocks=("thought",),
        tables=("thoughts",),
        api_patterns=(r"^/api/friends/[^/]+/thoughts(?:/|$)",),
    ),
    FeatureSpec(
        id="image",
        name="看图与画图",
        description="看她收到的图片、让她画图；关掉后不占聊天工具位。",
        category="聊天能力",
        tools=("draw_image", "look_image", "list_images"),
        api_patterns=(
            r"^/api/uploads/",
            r"^/api/images/",
            r"^/api/friends/[^/]+/draw_image$",
        ),
        ui_entries=("image",),
    ),
    FeatureSpec(
        id="speech_style",
        name="说话方式学习",
        description="从你的明确反馈里学她该怎么说话；带版本与回滚。",
        category="聊天能力",
        tools=("reflect_speech",),
        prompt_blocks=("style_profile",),
        tables=("friend_style_profiles", "friend_style_events"),
        api_patterns=(r"^/api/friends/[^/]+/style(?:/|$)",),
    ),
    FeatureSpec(
        id="schedule",
        name="日程",
        description="她自己的日程与到点提醒；关掉后不再扫日程、也不提示模型。",
        category="聊天能力",
        tools=("add_schedule", "list_schedules", "update_schedule", "remove_schedule"),
        prompt_blocks=("schedule",),
        jobs=("schedule_scan",),
        tables=("schedules", "schedule_events"),
        api_patterns=(
            r"^/api/schedules(?:/|$)",
            r"^/api/friends/[^/]+/schedule-events(?:/|$)",
        ),
        ui_entries=("schedule",),
    ),
    FeatureSpec(
        id="screen",
        name="看屏幕",
        description="经你允许后看一眼当前屏幕；只做手动/低频，不做实时盯屏。",
        category="聊天能力",
        tools=("look_screen",),
        prompt_blocks=("screen",),
        api_patterns=(r"^/api/focus(?:/|$)",),
        ui_entries=("screen",),
    ),
    FeatureSpec(
        id="search",
        name="联网搜索",
        description="让她替你查一下网上的事；关掉后聊天里不再出现搜索工具。",
        category="聊天能力",
        tools=("web_search",),
    ),
    FeatureSpec(
        id="knowledge",
        name="知识库",
        description="把文件发给她读；关掉后不注入资料、不检索片段。",
        category="聊天能力",
        tools=("search_knowledge",),
        prompt_blocks=("knowledge",),
        tables=("documents", "document_chunks"),
        api_patterns=(r"^/api/friends/[^/]+/documents(?:/|$)",),
        ui_entries=("knowledge",),
    ),
    FeatureSpec(
        id="files",
        name="文件操作",
        description="只读浏览整机、增删改逐笔点头；关掉后不出现文件工具。",
        category="聊天能力",
        tools=("list_dir", "read_file", "write_file", "move_path", "delete_path"),
        api_patterns=(r"^/api/approvals(?:/|$)",),
    ),
    FeatureSpec(
        id="game",
        name="游戏模式",
        description="一起玩 Minecraft；独立适配器进程，崩了不拖垮聊天。",
        category="玩法",
        tools=(
            "game_status",
            "game_inventory",
            "game_inspect",
            "game_survey",
            "game_do",
            "game_say",
            "game_cancel",
            "game_plan",
            "game_mode",
            "game_cheat",
            "game_skill",
            "game_build",
            "game_knowledge",
            "game_plugin",
        ),
        prompt_blocks=("game", "recent_games"),
        tables=(
            "game_bindings",
            "game_sessions",
            "game_tasks",
            "game_events",
            "game_skills",
            "game_skill_failures",
            "game_blueprints",
            "game_creative_records",
        ),
        api_patterns=(r"^/api/game(?:/|$)",),
        ui_entries=("game",),
    ),
    FeatureSpec(
        id="life",
        name="生活线",
        description="她自己的真实活动（B 站 / 搜索 / RSS），按朋友单独开关。",
        category="生活与陪伴",
        prompt_blocks=("life",),
        jobs=("life_tick", "life_share_tick"),
        tables=("friend_life", "life_projects", "life_activities"),
        api_patterns=(r"^/api/life(?:/|$)",),
        ui_entries=("life",),
    ),
    FeatureSpec(
        id="expression",
        name="表达学习",
        description="从你最近的说法里学短表达样本，让她说话别越来越像模板。",
        category="聊天能力",
        prompt_blocks=("expression",),
        jobs=("expression_learning",),
        tables=("friend_expressions",),
    ),
    FeatureSpec(
        id="portraits",
        name="立绘表情",
        description="立绘、表情与背景；关掉后不往聊天里注入可用表情。",
        category="生活与陪伴",
        prompt_blocks=("portrait",),
        api_patterns=(
            r"^/api/friends/[^/]+/portraits(?:/|$)",
            r"^/api/portrait-packs(?:/|$)",
        ),
        ui_entries=("portrait",),
    ),
    FeatureSpec(
        id="immersive",
        name="沉浸模式与通话",
        description="立绘整屏聊天与半双工语音通话；只是同一份聊天流的另一种呈现。",
        category="界面",
        api_patterns=(
            r"^/api/friends/[^/]+/live2d(?:/|$)",
            r"^/api/live2d/(?:models|runtime)(?:/|$)",
        ),
        ui_entries=("immersive", "call"),
    ),
    FeatureSpec(
        id="space",
        name="小世界",
        description="她自己会动、会被碰、道具会留痕的 3D 房间。",
        category="界面",
        prompt_blocks=("space",),
        tables=("friend_spaces", "space_events"),
        api_patterns=(
            r"^/api/friends/[^/]+/space(?:/|$)",
            r"^/api/friends/[^/]+/space-profile(?:/|$)",
        ),
        ui_entries=("space",),
    ),
    FeatureSpec(
        id="pet",
        name="桌宠",
        description="独立置顶小窗；关掉后聊天页不再提供「桌宠」入口。",
        category="界面",
        ui_entries=("pet",),
    ),
)

BY_ID: dict[str, FeatureSpec] = {feature.id: feature for feature in FEATURES}


def get_feature(feature_id: str) -> FeatureSpec | None:
    return BY_ID.get(feature_id)


def _index(attr: str) -> dict[str, str]:
    result: dict[str, str] = {}
    for feature in FEATURES:
        for value in getattr(feature, attr):
            result.setdefault(value, feature.id)
    return result


_TOOL_OWNER = _index("tools")
_PROMPT_OWNER = _index("prompt_blocks")
_JOB_OWNER = _index("jobs")


def feature_for_tool(tool_name: str) -> str | None:
    return _TOOL_OWNER.get(tool_name)


def feature_for_prompt_block(block: str) -> str | None:
    return _PROMPT_OWNER.get(block)


def feature_for_job(job_id: str) -> str | None:
    return _JOB_OWNER.get(job_id)


_PATH_RULES: tuple[tuple[re.Pattern[str], str], ...] = tuple(
    (re.compile(pattern), feature.id)
    for feature in FEATURES
    for pattern in feature.api_patterns
)


def feature_for_path(path: str) -> str | None:
    """返回这个 HTTP 路径属于哪个功能；没登记 = None（放行）。"""
    for pattern, feature_id in _PATH_RULES:
        if pattern.search(path):
            return feature_id
    return None
