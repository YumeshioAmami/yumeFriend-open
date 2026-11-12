"""全局设置读取（api.md §5）。

settings 是单行表（id 恒为 1）。M2 只用到 LLM 相关字段；
设置的读写接口与界面排在 T2.8。
"""

import json
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from backend import config
from backend.game.cheats import normalize_config
from backend.game.reflexes import normalize_config as normalize_reflex_config
from backend.models import Settings

SETTINGS_ID = 1

# v2（D80）：按用途的模型配置。顺序也是前端展示顺序。
PURPOSE_NAMES: tuple[str, ...] = (
    "chat",
    "game_fast",
    "vision",
    "tts",
    "stt",
    "image",
    "embedding",
)

# 没配过时的默认形状（只有"视觉跟随聊天"是默认打开的：D79 实测 chat 模型本身就能看图）
DEFAULT_PURPOSES: dict[str, dict] = {
    "chat": {},
    # D279：游戏快脑（本地 Ollama 可选）；没配就自动用聊天模型。
    "game_fast": {},
    "vision": {"follow_chat": True},
    "tts": {},
    "stt": {},
    "image": {},
    # D450：向量检索默认**关**、不自动下载；普通设置里可以选择下载增强模型。
    "embedding": {
        "enabled": False,
        "provider": "local",
        "protocol": "openai",
        "model": config.EMBEDDING_DEFAULT_MODEL,
        "dim": config.EMBEDDING_DEFAULT_DIM,
        "min_similarity": config.EMBEDDING_DEFAULT_MIN_SIMILARITY,
        "allow_download": False,
    },
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def get(session: Session) -> Settings:
    """取设置行；没有就现造一条默认的（首次启动 / 迁移后为空时不至于崩）。"""
    row = session.get(Settings, SETTINGS_ID)
    if row is None:
        row = Settings(
            id=SETTINGS_ID,
            active_message_frequency=60,
            deep_thinking=0,
            show_tool_calls=0,
            llm_bases="[]",
            llm_active=None,
            updated_at=_now(),
        )
        session.add(row)
        session.flush()
    return row


def llm_config(session: Session) -> tuple[str, str | None]:
    """返回 (provider 列表 JSON, active 名字)——**给现有的 LLM 调用方用**。

    v2 起这里优先读 `purposes.chat`（D80），读不到才退回老的 `llm_bases`：
    这样老调用点**一行都不用改**，过渡期也不会崩（迁移会把老配置搬进 purposes.chat）。
    """
    row = get(session)
    chat = purposes_from(row).get("chat")
    if isinstance(chat, dict) and chat.get("base_url") and chat.get("model"):
        item = {k: v for k, v in chat.items() if k != "mode"}
        item["name"] = "chat"
        return json.dumps([item], ensure_ascii=False), "chat"
    return row.llm_bases or "[]", row.llm_active


def purposes_from(row: Settings) -> dict:
    """解析 `purposes`（容错：坏 JSON / 不是对象都当空）。"""
    try:
        data = json.loads(row.purposes or "{}")
    except json.JSONDecodeError:
        data = {}
    return data if isinstance(data, dict) else {}


def public_purposes_from(row: Settings) -> dict[str, dict]:
    """给前端的用途配置：**api_key 永远不回明文**（同 llm_bases 的规矩，api.md §5.1），
    并且把 5 个用途**都补出来**（没配的给默认形状），前端渲染起来省事。
    """
    stored = purposes_from(row)
    result: dict[str, dict] = {}
    for name in PURPOSE_NAMES:
        conf = stored.get(name)
        item: dict = dict(conf) if isinstance(conf, dict) else {}
        if "api_key" in item:
            item["api_key_set"] = bool(item.pop("api_key"))
        elif name in stored:
            item["api_key_set"] = False
        result[name] = {**DEFAULT_PURPOSES.get(name, {}), **item}
    return result


def public_purposes(session: Session) -> dict[str, dict]:
    return public_purposes_from(get(session))


def game_cheats_from(row: Settings) -> dict:
    """读取并归一化作弊配置（D290）。坏 JSON 一律回到 survival + 全关。"""
    try:
        data = json.loads(row.game_cheats or "{}")
    except json.JSONDecodeError:
        data = {}
    return normalize_config(data)


def save_game_cheats(session: Session, config: dict) -> Settings:
    """用户确认“记住”时才写这里；LLM 没有调用入口。"""
    row = get(session)
    row.game_cheats = json.dumps(normalize_config(config), ensure_ascii=False)
    row.updated_at = _now()
    session.flush()
    return row


def game_reflexes_from(row: Settings) -> dict:
    try:
        data = json.loads(row.game_reflexes or "{}")
    except json.JSONDecodeError:
        data = {}
    return normalize_reflex_config(data)


def save_game_reflexes(session: Session, config: dict) -> Settings:
    row = get(session)
    row.game_reflexes = json.dumps(
        normalize_reflex_config(config), ensure_ascii=False
    )
    row.updated_at = _now()
    session.flush()
    return row


def features_raw_from(row: Settings) -> dict:
    """原始功能开关 JSON（保留未知键，方便以后回滚 / 兼容旧版本）。"""
    try:
        data = json.loads(getattr(row, "features", None) or "{}")
    except json.JSONDecodeError:
        data = {}
    return data if isinstance(data, dict) else {}


def set_feature(session: Session, feature_id: str, enabled: bool) -> Settings:
    """写一个功能开关；只动这一个键，不覆盖别人。"""
    row = get(session)
    data = features_raw_from(row)
    data[feature_id] = bool(enabled)
    row.features = json.dumps(data, ensure_ascii=False)
    row.updated_at = _now()
    session.flush()
    return row


def effective_purposes_from(row: Settings) -> dict[str, dict]:
    """**带默认形状**的用途配置——给 LLM 层解析用。

    为什么要单独一个函数：浏览器里"视觉跟随聊天"是**默认值**，
    而数据库里可能压根没有 `vision` 这一项（用户没动过）。
    解析时不吃到默认值，就会出现"设置界面显示跟随聊天、实际却报没配置"的鬼故事。
    """
    stored = purposes_from(row)
    merged: dict[str, dict] = {}
    for name in PURPOSE_NAMES:
        conf = stored.get(name)
        merged[name] = {**DEFAULT_PURPOSES.get(name, {}), **(conf if isinstance(conf, dict) else {})}
    return merged


# 「哪些用途必须配」——**只有 chat**（D83）。其它没配就是"没这个能力"，不是错误。
_LLM_SHAPED_PURPOSES = ("chat", "game_fast", "vision", "stt", "image")


def purpose_enabled_from(row: Settings, name: str) -> bool:
    """这个用途现在**能不能用**。前端拿它决定"给不给这个入口"。

    三态：没配 / 配好了 / 用 `enabled: false` 明确关掉（视同没配）。
    注意 TTS 的形状和 LLM 不一样（它是"服务地址 + 引擎"，没有 model）。
    """
    conf = effective_purposes_from(row).get(name)
    if not isinstance(conf, dict) or conf.get("enabled") is False:
        return False
    if conf.get("follow_chat"):
        return purpose_enabled_from(row, "chat")
    if name == "embedding":
        # D450：默认关；用户下载 / 配置后显式打开。
        return bool(conf.get("enabled", False))
    if name == "tts":
        # 语音这项的"启用"= 用户**指过路**（填了引擎/地址/程序目录之一）
        # 至于"服务现在开没开"，那是 status 的事，别混在一起（否则永远显示未启用）
        return bool(conf.get("engine") or conf.get("base_url") or conf.get("sovits_dir"))
    return bool(conf.get("base_url") and conf.get("model"))


def purposes_enabled_from(row: Settings) -> dict[str, bool]:
    return {name: purpose_enabled_from(row, name) for name in PURPOSE_NAMES}


def vision_follows_chat(row: Settings) -> bool:
    """聊天那一轮能不能**直接塞图片 parts**（T2.0-6 / D82 / D83）。

    只有"「视觉」启用了**并且**跟随聊天"才算 —— 那等于用户声明"我的聊天模型本身能看图"
    （D79 实测：现在这个 27B 就能看）。

    · 视觉没配 / 关掉 → False（她会知道"有张图我看不到"，如实说，不假装看懂）
    · 配了**独立**视觉模型 → v2.0 也返回 False：图片既不会塞给 chat，也不会转发给那个模型
      （把图转给独立视觉模型再回灌，是 v2.1 的事，记在 TASKS 里）
    """
    purposes = effective_purposes_from(row)
    vision = purposes.get("vision") or {}
    if not isinstance(vision, dict) or not vision.get("follow_chat"):
        return False
    return purpose_enabled_from(row, "vision")


def merge_purposes(session: Session, incoming: dict[str, dict]) -> Settings:
    """按用途合并写入。`api_key` 的语义跟 llm_bases 一致（api.md §5.2）：

    · 字段**不出现** → 沿用旧值（前端拿不到明文 key，不回传就是"不改"）
    · 传空字符串   → 清空
    · 传非空       → 覆盖
    """
    row = get(session)
    existing = purposes_from(row)
    merged = dict(existing)
    for name, conf in incoming.items():
        if not isinstance(conf, dict):
            continue
        old = existing.get(name)
        old = old if isinstance(old, dict) else {}
        item = {k: v for k, v in conf.items() if v is not None}
        # 只有 LLM 那几项才有 api_key；tts 别被塞一个空 key（不然前端会多显示个"密钥未设"）
        if name in _LLM_SHAPED_PURPOSES and "api_key" not in item:
            item["api_key"] = old.get("api_key", "")
        # ⚠️ 要**合并进旧配置**，不是整个替换：
        # 前端有时只传一部分字段（比如扫描时只补一个 sovits_dir），
        # 整个替换会把 base_url / sample_steps 一起抹掉（踩过一次）。
        merged[name] = {**old, **item}
    row.purposes = json.dumps(merged, ensure_ascii=False)
    row.updated_at = _now()
    session.flush()
    return row


def set_llm_bases(session: Session, bases: list[dict], active: str | None = None) -> Settings:
    """写回 provider 配置（T2.8 的接口会用；现在测试与脚本也靠它）。"""
    row = get(session)
    row.llm_bases = json.dumps(bases, ensure_ascii=False)
    if active is not None:
        row.llm_active = active
    row.updated_at = _now()
    session.flush()
    return row


def public_llm_bases_from(row: Settings) -> list[dict[str, object]]:
    """给前端的 provider 列表：**绝不回明文 key**，只回 `api_key_set`（api.md §5.1）。"""
    try:
        bases = json.loads(row.llm_bases or "[]")
    except json.JSONDecodeError:
        return []
    if not isinstance(bases, list):
        return []
    result: list[dict[str, object]] = []
    for base in bases:
        if not isinstance(base, dict):
            continue
        item = {key: value for key, value in base.items() if key != "api_key"}
        item["api_key_set"] = bool(base.get("api_key"))
        result.append(item)
    return result


def llm_bases_from(row: Settings) -> list[dict]:
    """`llm_bases` 的原文解析（容错）—— v3.5 起它就是"**模型预设**"列表（D151）。"""
    try:
        data = json.loads(row.llm_bases or "[]")
    except json.JSONDecodeError:
        return []
    return [item for item in data if isinstance(item, dict)] if isinstance(data, list) else []


def llm_base_from(row: Settings, name: str | None) -> dict | None:
    """按名字取一个预设（找不到给 None —— 报错交给 `factory.resolve_base_config`，口径只有一处）。"""
    wanted = (name or "").strip()
    if not wanted:
        return None
    for item in llm_bases_from(row):
        if str(item.get("name") or "") == wanted:
            return item
    return None


def vision_for_friend(row: Settings, friend) -> bool:  # noqa: ANN001 —— Friend（避免循环 import）
    """**这个朋友这条链路看不看得见图**（D151）。

    朋友自己指定了端点 → 看**那个端点**声明的 `capabilities.vision`；
    没指定 → 沿用全局「视觉跟随聊天」（D79/D83/D82）。
    为什么要分开判：把一个看不见图的模型配上图 = 白烧 token / 直接报错。
    """
    conf = llm_base_from(row, getattr(friend, "llm_base", None))
    if conf is not None:
        return bool((conf.get("capabilities") or {}).get("vision"))
    return vision_follows_chat(row)


def public_llm_bases(session: Session) -> list[dict[str, object]]:
    return public_llm_bases_from(get(session))


def merge_llm_bases(
    session: Session, incoming: list[dict], *, active: str | None = None
) -> Settings:
    """PATCH 用的合并写入。

    `api_key` 的语义（避免前端拿不到明文就无法保存）：
    · 字段**不出现在**请求里（None）→ 沿用旧的
    · 传空字符串 → 清空
    · 传非空 → 覆盖
    """
    row = get(session)
    try:
        existing = json.loads(row.llm_bases or "[]")
    except json.JSONDecodeError:
        existing = []
    old_keys = {
        base.get("name"): base.get("api_key", "")
        for base in existing
        if isinstance(base, dict)
    }

    merged: list[dict] = []
    for item in incoming:
        base = {key: value for key, value in item.items() if value is not None}
        if "api_key" not in base:
            base["api_key"] = old_keys.get(base.get("name", ""), "")
        merged.append(base)

    row.llm_bases = json.dumps(merged, ensure_ascii=False)
    if active is not None:
        row.llm_active = active
    elif row.llm_active and row.llm_active not in {b.get("name") for b in merged}:
        row.llm_active = merged[0].get("name") if merged else None
    row.updated_at = _now()
    session.flush()
    return row


def patch_settings(session: Session, **fields) -> Settings:
    """按字段更新（None 表示"这次不改"）。"""
    row = get(session)
    for key, value in fields.items():
        if value is None:
            continue
        if key in (
            "deep_thinking",
            "show_tool_calls",
            "warmup_enabled",
            "file_write_approval",
            "screen_watch_enabled",
            "auto_start",
        ):
            setattr(row, key, 1 if value else 0)
        else:
            setattr(row, key, value)
    row.updated_at = _now()
    session.flush()
    # v3.15（2026-09-26）：开机自启是"碰文件系统"的副作用，交给 autostart_service。
    # 跑 pytest 时它会自己拒绝动真·启动文件夹（D179 的教训：测试绝不碰真环境）。
    if fields.get("auto_start") is not None:
        from backend.services import autostart_service  # 懒导入，避免循环

        autostart_service.apply(bool(row.auto_start))
    return row


def setup_status(session: Session) -> dict:
    """首次配置检测（api.md §5.4）—— 让前端知道该不该弹设置引导。

    v2 起优先看 `purposes.chat`；它没配好就退回老字段（过渡期两套都得认）。
    返回的 `missing` 标签沿用 v1 的名字，前端不用改。
    """
    row = get(session)
    missing: list[str] = []
    chat = effective_purposes_from(row).get("chat") or {}
    if chat.get("base_url") and chat.get("model"):
        return {"needs_setup": False, "missing": []}
    try:
        bases = json.loads(row.llm_bases or "[]")
    except json.JSONDecodeError:
        bases = []
    usable = [
        base
        for base in bases
        if isinstance(base, dict) and base.get("base_url") and base.get("model")
    ]
    if not usable:
        missing.append("llm_bases")
    if not row.llm_active or row.llm_active not in {b.get("name") for b in usable}:
        missing.append("llm_active")
    return {"needs_setup": bool(missing), "missing": missing}
