"""全局测试保护：**任何测试都不许碰真实的 `user_data/`**。

2026-09-21 踩到的真事故：`test_cleanup_job` 调了每日清理任务，而那条任务现在会清
"消息语音 / 合成缓存 / 孤儿图片"。那个测试 monkeypatch 了数据库和大部分目录，
**漏了 `AUDIO_DIR` / `TTS_CACHE_DIR`** → 清理任务把**真实** `user_data/audio/` 里
117 MB 语音当成"没有消息引用的孤儿"全删了。

教训：**目录级的 monkeypatch 靠人记，一定会漏** → 这里统一兜住：
每个测试默认就被指到一个隔离的临时 user_data 里，想用真实目录得自己显式改回来
（正常没人会这么干）。清理类函数另有一道运行期安全闸（`config.cleanup_targets_are_consistent`）。
"""

from __future__ import annotations

import pytest

import backend.config as config
import backend.services.tts_service as tts_service
from backend.services import usage_service


@pytest.fixture(autouse=True)
def _no_real_tts_service(monkeypatch):
    """**任何测试都不许连真的语音服务**（2026-09-23 真事故的第二集）。

    事故经过：`tts_settings()` 的**默认地址就是本机 9880** → 那些"没配 TTS 用途"的测试
    会真的去打用户开着的 GPT-SoVITS；而服务端会把 `set_gpt_weights` 的路径
    **写进它自己的配置文件**（实测被写成了 pytest 临时路径）→
    用户下次点启动器直接崩（`UnpicklingError: invalid load key, 'f'`）。

    所以这里统一把默认地址指到 9 端口（永远拒绝连接、也不出网），并清掉缓存的 client。
    真要测 HTTP 行为的用例，自己 monkeypatch `tts_service._http`（照 `test_voices.py` 那样）。
    """
    monkeypatch.setattr(tts_service, "DEFAULT_BASE_URL", "http://127.0.0.1:9")
    monkeypatch.setattr(tts_service, "_client", None)
    yield


@pytest.fixture(autouse=True)
def _isolated_user_data(tmp_path_factory, monkeypatch):
    """把 user_data 及其所有子目录都指到临时目录（**每一个测试都自动生效**）。

    ⚠️ 用 `tmp_path_factory` 而不是 `tmp_path`：后者是**这个测试自己的**临时目录，
    往里塞东西会污染那些"列目录看内容"的测试（踩过：files_approval 那条就多出一个条目）。
    """
    root = tmp_path_factory.mktemp("user-data-isolated")
    root.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(config, "USER_DATA_DIR", root)
    monkeypatch.setattr(config, "LOG_DIR", root / "logs")
    monkeypatch.setattr(config, "AVATAR_DIR", root / "avatars")
    monkeypatch.setattr(config, "AVATAR_MODEL_DIR", root / "avatar_models")
    monkeypatch.setattr(config, "LIVE2D_DIR", root / "live2d")
    monkeypatch.setattr(config, "LIVE2D_FRIEND_DIR", root / "live2d" / "friends")
    monkeypatch.setattr(config, "AUDIO_DIR", root / "audio")
    monkeypatch.setattr(config, "TTS_CACHE_DIR", root / "tts_cache")
    monkeypatch.setattr(config, "IMAGE_DIR", root / "images")
    monkeypatch.setattr(config, "UPLOAD_PENDING_DIR", root / "uploads" / "pending")
    monkeypatch.setattr(config, "DOCUMENT_DIR", root / "documents")
    monkeypatch.setattr(config, "PORTRAIT_DIR", root / "portraits")
    monkeypatch.setattr(config, "PORTRAIT_PACK_DIR", root / "portrait_packs")
    monkeypatch.setattr(config, "BACKGROUND_DIR", root / "backgrounds")
    monkeypatch.setattr(config, "SPACE_ASSET_DIR", root / "space_assets")
    monkeypatch.setattr(usage_service, "USAGE_LOG_PATH", root / "logs" / "usage.jsonl")
    # 默认朋友由真实启动流程创建；测试里单独建库，别让 lifespan 多塞一个朋友。
    monkeypatch.setenv("YF_SKIP_DEFAULT_FRIEND", "1")
    yield
