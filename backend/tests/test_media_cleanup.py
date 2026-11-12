"""v3.0 / D141：语音文件与孤儿图片的定时清理。

盯四件事（按风险从大到小）：

1. **不该删的一个都不许动**：没到期的语音、最近 200 条消息的语音、还被引用的图片
2. 该删的三类：孤儿（消息没了）、过期（超过保留天数）、超量（超过总量上限）
3. **彻底删朋友时，她的图片和语音文件一起清掉**（宪法§9：不许留 orphan）——这是以前漏的
4. 合成缓存按"多久没用过"清，而且**命中的时候要刷 mtime**（不然天天用的那句话也会被删）
"""

from __future__ import annotations

import os
import time
from datetime import datetime, timedelta, timezone

import pytest

import backend.config as config
import backend.db as db
from backend.models import Friend, Message
from backend.services import friend_service, image_service, tts_service, voice_reply_service
from backend.services.actor import Actor


def _ago(days: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()


def _touch_old(path, days: float) -> None:
    """把文件的 mtime 拨到 N 天前（清理就是按它判断"多久没动过"）。"""
    stamp = time.time() - days * 86400
    os.utime(path, (stamp, stamp))


@pytest.fixture()
def user_data(tmp_path, monkeypatch):
    db_file = tmp_path / "media.db"
    monkeypatch.setattr(config, "DB_PATH", db_file)
    monkeypatch.setattr(db, "DB_PATH", db_file)
    monkeypatch.setattr(db, "_engine", None)
    monkeypatch.setattr(db, "_session_factory", None)
    monkeypatch.setattr(config, "USER_DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "AUDIO_DIR", tmp_path / "audio")
    monkeypatch.setattr(config, "IMAGE_DIR", tmp_path / "images")
    monkeypatch.setattr(config, "UPLOAD_PENDING_DIR", tmp_path / "uploads" / "pending")
    monkeypatch.setattr(config, "TTS_CACHE_DIR", tmp_path / "tts_cache")
    monkeypatch.setattr(config, "AVATAR_DIR", tmp_path / "avatars")
    monkeypatch.setattr(config, "PORTRAIT_DIR", tmp_path / "portraits")
    monkeypatch.setattr(config, "BACKGROUND_DIR", tmp_path / "backgrounds")
    monkeypatch.setattr(config, "DOCUMENT_DIR", tmp_path / "documents")
    from backend.db import Base, get_engine

    Base.metadata.create_all(get_engine())
    config.AUDIO_DIR.mkdir(parents=True, exist_ok=True)
    config.IMAGE_DIR.mkdir(parents=True, exist_ok=True)
    config.TTS_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    return tmp_path


def _add_message(session, mid: str, *, friend_id: str = "f1", created: float = 0, audio: bool = False,
                 image: str | None = None) -> None:
    if not session.get(Friend, friend_id):
        session.add(
            Friend(
                id=friend_id,
                name=f"朋友{friend_id}",
                created_at=_ago(0),
                updated_at=_ago(0),
            )
        )
        session.flush()
    session.add(
        Message(
            id=mid,
            friend_id=friend_id,
            role="assistant",
            content="你好",
            created_at=_ago(created),
            audio_status="ready" if audio else None,
            audio_path=f"audio/{mid}.wav" if audio else None,
            image_path=image,
        )
    )


def _write_audio(mid: str, *, days: float = 0) -> None:
    path = voice_reply_service.audio_path_of(mid)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"RIFFaudio")
    if days:
        _touch_old(path, days)


# ---------- 该删的 / 不该删的 ----------


def test_audio_cleanup_keeps_the_recent_and_deletes_the_old(user_data):
    with db.session_scope() as session:
        _add_message(session, "recent", created=1, audio=True)   # 昨天，文件也新
        _add_message(session, "old", created=40, audio=True)     # 40 天前
    _write_audio("recent")
    _write_audio("old", days=40)

    with db.session_scope() as session:
        # keep_recent=1：只保护"最新那一条"，好让"保护名单"和"过期"这两条规则分开验
        result = voice_reply_service.purge_audio(session, keep_days=30, keep_recent=1)

    assert result["expired"] == 1 and result["removed"] == 1
    assert voice_reply_service.audio_path_of("recent").exists()
    assert not voice_reply_service.audio_path_of("old").exists()


def test_recent_200_messages_are_protected(user_data):
    """最近 200 条有语音的消息：哪怕文件很老也留着（免得翻记录全是"重新合成"）。"""
    with db.session_scope() as session:
        _add_message(session, "just-talked", created=60, audio=True)  # 60 天前，但在最近 200 条里
    _write_audio("just-talked", days=60)

    with db.session_scope() as session:
        result = voice_reply_service.purge_audio(session, keep_days=30, keep_recent=200)

    assert result["removed"] == 0
    assert voice_reply_service.audio_path_of("just-talked").exists()


def test_audio_orphans_are_deleted(user_data):
    """消息行没了（删过朋友）→ 文件必须删（宪法§9 不留 orphan）。"""
    _write_audio("ghost", days=1)
    (config.AUDIO_DIR / "ghost-dir").mkdir()
    (config.AUDIO_DIR / "ghost-dir" / "0.wav").write_bytes(b"RIFF")

    with db.session_scope() as session:
        result = voice_reply_service.purge_audio(session)

    assert result["orphans"] == 2
    assert not voice_reply_service.audio_path_of("ghost").exists()
    assert not (config.AUDIO_DIR / "ghost-dir").exists()


def test_audio_size_cap_deletes_the_oldest(user_data):
    """总量上限：超了从最旧的删，删除量刚好压到线下。"""
    with db.session_scope() as session:
        for index in range(4):
            _add_message(session, f"m{index}", created=index + 1, audio=True)
    for index in range(4):
        _write_audio(f"m{index}", days=4 - index)  # m3 最新、m0 最旧

    with db.session_scope() as session:
        result = voice_reply_service.purge_audio(
            session, keep_days=0, keep_recent=0, max_bytes=2 * len(b"RIFFaudio")
        )

    assert result["oversize"] == 2
    assert not voice_reply_service.audio_path_of("m0").exists()
    assert not voice_reply_service.audio_path_of("m1").exists()
    assert voice_reply_service.audio_path_of("m2").exists()
    assert voice_reply_service.audio_path_of("m3").exists()


def test_keep_days_zero_means_keep_forever(user_data):
    with db.session_scope() as session:
        _add_message(session, "ancient", created=999, audio=True)
    _write_audio("ancient", days=999)

    with db.session_scope() as session:
        result = voice_reply_service.purge_audio(session, keep_days=0, keep_recent=0)

    assert result["removed"] == 0
    assert voice_reply_service.audio_path_of("ancient").exists()


# ---------- 图片孤儿 ----------


def test_image_orphans_are_deleted_but_referenced_ones_stay(user_data):
    keep = config.IMAGE_DIR / ("a" * 32 + ".png")
    keep.write_bytes(b"png")
    orphan = config.IMAGE_DIR / ("b" * 32 + ".png")
    orphan.write_bytes(b"png")
    with db.session_scope() as session:
        _add_message(session, "m1", image=f"images/{keep.name}")

    with db.session_scope() as session:
        removed = image_service.purge_orphans(session)

    assert removed == 1
    assert keep.exists(), "还被消息引用的图不许删"
    assert not orphan.exists()


# ---------- 彻底删朋友：文件一起清 ----------


def test_purge_friend_removes_images_and_audio(user_data):
    """这条是**以前漏掉的真 bug**：彻底删朋友时 images/ 和 audio/ 会留下孤儿。"""
    image = config.IMAGE_DIR / ("c" * 32 + ".png")
    image.write_bytes(b"png")
    with db.session_scope() as session:
        _add_message(session, "m1", image=f"images/{image.name}")
        _add_message(session, "m2", audio=True)
    _write_audio("m2")
    segment_dir = voice_reply_service.segment_dir_of("m2")
    segment_dir.mkdir(parents=True, exist_ok=True)
    (segment_dir / "0.wav").write_bytes(b"RIFF")

    with db.session_scope() as session:
        friend_service.purge(session, Actor.USER, "f1")

    assert not image.exists(), "她的图片要跟着删"
    assert not voice_reply_service.audio_path_of("m2").exists(), "她的语音要跟着删"
    assert not segment_dir.exists(), "分段语音目录也要删"
    with db.session_scope() as session:
        assert session.query(Message).count() == 0


# ---------- 合成缓存 ----------


def test_tts_cache_cleanup_by_age_and_size(user_data):
    fresh = config.TTS_CACHE_DIR / "fresh.wav"
    fresh.write_bytes(b"x" * 10)
    stale = config.TTS_CACHE_DIR / "stale.wav"
    stale.write_bytes(b"x" * 10)
    _touch_old(stale, days=5)

    result = tts_service.purge_cache(keep_days=3)

    assert result["removed"] == 1
    assert fresh.exists() and not stale.exists()

    # 按总量：三个各 10 字节、上限 20 → 至少删掉一个（从最旧的开始）
    for name in ("a", "b", "c"):
        path = config.TTS_CACHE_DIR / f"{name}.wav"
        path.write_bytes(b"x" * 10)
        _touch_old(path, days={"a": 3, "b": 2, "c": 1}[name])
    sized = tts_service.purge_cache(keep_days=0, max_bytes=20)

    assert sized["removed"] >= 1
    total = sum(item.stat().st_size for item in config.TTS_CACHE_DIR.glob("*.wav"))
    assert total <= 20 or sized["removed"] == 0


def test_cache_hit_refreshes_mtime(user_data, monkeypatch):
    """命中缓存要刷 mtime —— 不然"天天用的那句话"会按创建时间被当老缓存删掉。"""
    from backend.models import Voice

    voice = Voice(
        id="v1",
        name="测试音色",
        gpt_weights_path="gpt.ckpt",
        sovits_weights_path="sovits.pth",
        ref_audio_path="ref.wav",
        prompt_text="台词",
        speed=1.0,
        created_at=_ago(1),
        updated_at=_ago(1),
    )
    name = tts_service.cache_name_of(text="你好呀", voice=voice)
    cached = config.TTS_CACHE_DIR / f"{name}.wav"
    cached.write_bytes(b"RIFFcached")
    _touch_old(cached, days=10)

    monkeypatch.setattr(tts_service, "_ensure_weights", lambda *a, **k: None)
    raw = tts_service.synthesize(text="你好呀", voice=voice, base_url="http://127.0.0.1:9")

    assert raw == b"RIFFcached", "应该直接命中缓存（没去连上游）"
    age_days = (time.time() - cached.stat().st_mtime) / 86400
    assert age_days < 0.01, "命中缓存要刷新 mtime"


# ---------- 安全闸（2026-09-21 的事故不许再发生）----------


def test_cleanup_refuses_when_db_and_media_dir_disagree(user_data, monkeypatch, tmp_path):
    """复现那次事故的组合：**库在 A、语音目录在 B** → 一个文件都不许删。

    （当时是测试漏了 monkeypatch `AUDIO_DIR`：库指临时目录、目录指真实 user_data，
    清理任务就把真实语音全当孤儿删了。）
    """
    _write_audio("looks-like-orphan", days=1)
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "somewhere-else" / "x.db")

    with db.session_scope() as session:
        result = voice_reply_service.purge_audio(session)
        images = image_service.purge_orphans(session)

    assert result["removed"] == 0 and images == 0
    assert voice_reply_service.audio_path_of("looks-like-orphan").exists()


def test_tests_never_run_against_the_real_user_data():
    """全局兜底（conftest）：测试里的 user_data 一定不在仓库里。"""
    resolved = config.USER_DATA_DIR.resolve()
    assert resolved != (config.ROOT_DIR / "user_data").resolve()
    assert config.ROOT_DIR.resolve() not in resolved.parents
