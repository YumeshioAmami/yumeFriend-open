"""全局配置：路径与常量。

约定见 AGENTS.md §6：
  - 后端只绑 127.0.0.1（api.md §0.2）
  - 用户数据放仓库根的 user_data/（已 gitignore，打包后位于 exe 外部）
"""

import os
import sys
from pathlib import Path


def is_frozen() -> bool:
    """是不是 PyInstaller 打包出来在跑（`sys.frozen` 是它的标志）。"""
    return bool(getattr(sys, "frozen", False))


def resolve_resource_dir(meipass: str | None, root: Path) -> Path:
    """只读资源目录：打包给了解包目录就用它，否则用源码根。"""
    return Path(meipass) if meipass else root


def resolve_user_data_dir(frozen: bool, exe: Path, root: Path) -> Path:
    """可写数据目录：打包后放 exe 旁边（绿色版），开发时放仓库根。"""
    return (exe.parent / "user_data") if frozen else (root / "user_data")


# 仓库根目录（backend/ 的上一级）——**开发时的源码根**
ROOT_DIR = Path(__file__).resolve().parent.parent

# **只读资源**（alembic.ini / migrations / frontend/dist / assets / 语音小助手脚本）：
#   开发时就在仓库根；打包后 = PyInstaller 的解包目录（spec 里用 `datas` 塞进去）
RESOURCE_DIR = resolve_resource_dir(getattr(sys, "_MEIPASS", None), ROOT_DIR)

# **可写数据**（数据库、日志、图片、音频…）：
#   打包后**必须落在 exe 外面**，否则写进临时解包目录 → 一重启全没（M7 留的坑，v3.0 填）
#   绿色版策略（用户 2026-09-21 拍板）：exe 旁边一个 `user_data/`
#   开发/验收时可以用 `YF_USER_DATA` 显式指到某个 user_data（例如打包版旁边那份真数据）
_env_user_data = os.environ.get("YF_USER_DATA")
USER_DATA_DIR = (
    Path(_env_user_data).expanduser().resolve()
    if _env_user_data
    else resolve_user_data_dir(is_frozen(), Path(sys.executable).resolve(), ROOT_DIR)
)
DB_PATH = USER_DATA_DIR / "yumefriend.db"
LOG_DIR = USER_DATA_DIR / "logs"
AVATAR_DIR = USER_DATA_DIR / "avatars"  # 朋友头像（v2）
AVATAR_MODEL_DIR = USER_DATA_DIR / "avatar_models"  # 每个朋友可选的 VRM 模型
LIVE2D_DIR = USER_DATA_DIR / "live2d"  # 桌宠 Live2D 模型（D424；模型本体不进仓库）
LIVE2D_FRIEND_DIR = LIVE2D_DIR / "friends"  # 每个朋友可选的 Live2D 覆盖（D425）
LIVE2D_ASSETS_DIR = RESOURCE_DIR / "assets" / "live2d"  # 随仓库分发的模型
DEFAULT_LIVE2D_DIR = LIVE2D_ASSETS_DIR / "yui"  # Apache-2.0 深色默认模型（D446）
BUNDLED_LIVE2D_SETTINGS_DIR = LIVE2D_ASSETS_DIR / "model_settings"  # 默认模型的预置设置
AUDIO_DIR = USER_DATA_DIR / "audio"  # 语音消息的音频（v2）
TTS_CACHE_DIR = USER_DATA_DIR / "tts_cache"  # 合成缓存（v2）
IMAGE_DIR = USER_DATA_DIR / "images"  # 已经发出去的图（v2 / T2.0-6）
UPLOAD_PENDING_DIR = USER_DATA_DIR / "uploads" / "pending"  # 传了还没发的图（24h 没人要就清）
DOCUMENT_DIR = USER_DATA_DIR / "documents"  # 知识库的原件（v2.1 / T2.1-3）
PORTRAIT_DIR = USER_DATA_DIR / "portraits"  # 立绘（v2.2 / T2.2-3；一个朋友一个目录）
PORTRAIT_PACK_DIR = USER_DATA_DIR / "portrait_packs"  # 全局立绘包（v7.43 / D441）
BACKGROUND_DIR = USER_DATA_DIR / "backgrounds"  # 沉浸模式背景图（v2.2 / D107；一个朋友一张）
SPACE_ASSET_DIR = USER_DATA_DIR / "space_assets"  # 小世界导入的 GLB（v7.23 / D376）
MINECRAFT_DIR = USER_DATA_DIR / "minecraft"
MINECRAFT_VERSION_DIR = MINECRAFT_DIR / "versions"  # 按需下载的 minecraft-data 版本包

# v3.46（D231）：本地嵌入模型（ONNX / fastembed）的缓存目录。
# 放**用户级目录**、不进 user_data —— 模型 ~92MB，塞进 user_data 会让每次打包 stash / 手工备份
# 都搬一遍；放这儿是"一台机器下一份"，开发版和打包版共用。
MODEL_CACHE_DIR = Path(os.environ.get("LOCALAPPDATA") or Path.home()) / "yumeFriend" / "models"

# D450：向量检索模型改为普通设置里的**可选下载项**。默认推荐 Jina 中文增强模型。
EMBEDDING_DEFAULT_MODEL = "jinaai/jina-embeddings-v2-base-zh-quantized"
EMBEDDING_MODEL_SOURCE_REPO = "jinaai/jina-embeddings-v2-base-zh"
EMBEDDING_DEFAULT_DIM = 768
EMBEDDING_DEFAULT_MIN_SIMILARITY = 0.45
EMBEDDING_MODEL_DOWNLOAD_URL = (
    "https://www.yumeshio.cn/uploads/yumefriend/models/"
    "jina-embeddings-v2-base-zh-quantized-fastembed.zip"
)
EMBEDDING_MODEL_DOWNLOAD_SHA256 = (
    "8c76860de74bdcc6b01a8cfeeb01e057a506404bba9475b9c919b81dcca1fe6b"
)
EMBEDDING_MODEL_DOWNLOAD_SIZE_MB = 157

# v7.12（D342）：服务器模式是唯一允许显式绑非本机的场景。
# 普通桌面/手机端仍固定 127.0.0.1。
SERVER_ONLY = (os.environ.get("YF_SERVER_ONLY") or "").strip().lower() in {
    "1",
    "true",
    "yes",
}
HOST = (
    os.environ.get("YF_SERVER_HOST", "0.0.0.0")
    if SERVER_ONLY
    else "127.0.0.1"
)
# D283：固定 8001，避免手机端地址随端口漂移（8000 常有别的项目在用）。
PORT = int(os.environ.get("YF_PORT", "8001"))

APP_NAME = "yumeFriend"
APP_VERSION = "7.28.1"  # v7.28.1 手机端旧数据库升级兼容

# D456：桌面自动更新。发布清单和资源页都在 yumeBlog。
UPDATE_MANIFEST_URL = (
    os.environ.get("YF_UPDATE_MANIFEST_URL")
    or "https://www.yumeshio.cn/uploads/yumefriend/latest.json"
)
UPDATE_PAGE_URL = (
    os.environ.get("YF_UPDATE_PAGE_URL")
    or "https://www.yumeshio.cn/uploads/yumefriend/"
)
MINECRAFT_VERSION_MANIFEST_URL = (
    os.environ.get("YF_MINECRAFT_VERSION_MANIFEST_URL")
    or "https://www.yumeshio.cn/uploads/yumefriend/minecraft/versions.json"
)
UPDATER_EXE_NAME = "yumeFriend-updater.exe"
UPDATE_ALLOW_SOURCE = (os.environ.get("YF_UPDATE_ALLOW_SOURCE") or "").strip().lower() in {
    "1",
    "true",
    "yes",
}

# 定时任务参数（architecture.md §8）。测试里会改小，所以放常量而不是写死。
CATCHUP_POLL_SECONDS = 30  # 多久检查一次"停歇了没"
CATCHUP_IDLE_SECONDS = 5 * 60  # 停歇多久算"聊完了"（D24）
CATCHUP_MAX_MESSAGES = 60  # 一次补漏最多看多少条消息
# 清理任务多久跑一次（原来是"每天 4 点"→ 桌面版晚上就关了，永远赶不上；见 D141）
CLEANUP_EVERY_HOURS = 6

# 主动消息（D17 / D21 / D22）
HEARTBEAT_IDLE_SECONDS = 5 * 60  # 有人正在聊 → 全体静默（D17）
PROACTIVE_DICE_CHANCE = 0.5  # 每个在场朋友独立掷骰子的命中率（D21）


def ensure_dirs() -> None:
    """确保用户数据目录存在。"""
    USER_DATA_DIR.mkdir(parents=True, exist_ok=True)
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    AVATAR_DIR.mkdir(parents=True, exist_ok=True)
    AVATAR_MODEL_DIR.mkdir(parents=True, exist_ok=True)
    LIVE2D_DIR.mkdir(parents=True, exist_ok=True)
    MINECRAFT_VERSION_DIR.mkdir(parents=True, exist_ok=True)
    LIVE2D_FRIEND_DIR.mkdir(parents=True, exist_ok=True)
    AUDIO_DIR.mkdir(parents=True, exist_ok=True)
    TTS_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    IMAGE_DIR.mkdir(parents=True, exist_ok=True)
    UPLOAD_PENDING_DIR.mkdir(parents=True, exist_ok=True)
    DOCUMENT_DIR.mkdir(parents=True, exist_ok=True)
    PORTRAIT_DIR.mkdir(parents=True, exist_ok=True)
    PORTRAIT_PACK_DIR.mkdir(parents=True, exist_ok=True)
    BACKGROUND_DIR.mkdir(parents=True, exist_ok=True)
    SPACE_ASSET_DIR.mkdir(parents=True, exist_ok=True)
    MODEL_CACHE_DIR.mkdir(parents=True, exist_ok=True)


def cleanup_targets_are_consistent() -> bool:
    """**删除类清理的安全闸**：数据库 + 三个被清理的媒体目录必须都在同一个 user_data 下。

    为什么要有它（2026-09-21 真事故）：库指向临时目录、`AUDIO_DIR` 却指向真实的
    `user_data/audio`（测试漏 monkeypatch 时就是这个样子）→ 清理任务把真实目录里
    **所有**语音当成"没有消息引用的孤儿"删掉了，117 MB。有了这道闸，这种组合直接拒绝清理。
    """
    base = Path(USER_DATA_DIR).resolve()
    db = Path(DB_PATH).resolve()
    if db.parent != base:
        return False
    for folder in (AUDIO_DIR, IMAGE_DIR, TTS_CACHE_DIR):
        try:
            Path(folder).resolve().relative_to(base)
        except ValueError:
            return False
    return True
