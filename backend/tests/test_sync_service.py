"""三设备同步逻辑包的隔离测试（D302）。"""

from __future__ import annotations

import json
import uuid
from pathlib import Path

import pytest
from sqlalchemy import select

import backend.config as config
import backend.db as db
from backend import models  # noqa: F401
from backend.models import Friend, Memory, Message, Settings, SyncTombstone
from backend.services import sync_service


def _now() -> str:
    return "2026-10-22T00:00:00+00:00"


@pytest.fixture()
def two_dbs(tmp_path, monkeypatch):
    def make(name: str):
        root = tmp_path / name
        root.mkdir()
        monkeypatch.setattr(config, "USER_DATA_DIR", root)
        for attr in (
            "LOG_DIR",
            "AVATAR_DIR",
            "AUDIO_DIR",
            "TTS_CACHE_DIR",
            "IMAGE_DIR",
            "UPLOAD_PENDING_DIR",
            "DOCUMENT_DIR",
            "PORTRAIT_DIR",
            "PORTRAIT_PACK_DIR",
            "LIVE2D_DIR",
            "LIVE2D_FRIEND_DIR",
            "BACKGROUND_DIR",
        ):
            setattr(config, attr, root / attr.lower())
        config.ensure_dirs()
        db_path = root / "yumefriend.db"
        monkeypatch.setattr(config, "DB_PATH", db_path)
        monkeypatch.setattr(db, "DB_PATH", db_path)
        db._engine = None
        db._session_factory = None
        from backend.db import Base, get_engine

        Base.metadata.create_all(bind=get_engine())
        return root

    left = make("left")
    right = make("right")
    return left, right


def _add_friend(session, name="阿月") -> str:
    fid = str(uuid.uuid4())
    session.add(
        Friend(
            id=fid,
            name=name,
            personality="爱用括号写动作。",
            custom_instructions="",
            presence=1,
            current_mode="normal",
            unread_count=0,
            voice_reply_enabled=0,
            voice_examples="[]",
            created_at=_now(),
            updated_at=_now(),
        )
    )
    session.flush()
    return fid


def _use(root: Path) -> None:
    """把 config/db 全局切到某个隔离库（sync_service 直接读 config）。"""
    config.USER_DATA_DIR = root
    for attr in (
        "LOG_DIR",
        "AVATAR_DIR",
        "AUDIO_DIR",
        "TTS_CACHE_DIR",
        "IMAGE_DIR",
        "UPLOAD_PENDING_DIR",
        "DOCUMENT_DIR",
        "PORTRAIT_DIR",
        "PORTRAIT_PACK_DIR",
        "LIVE2D_DIR",
        "LIVE2D_FRIEND_DIR",
        "BACKGROUND_DIR",
    ):
        setattr(config, attr, root / attr.lower())
    config.ensure_dirs()
    db_path = root / "yumefriend.db"
    config.DB_PATH = db_path
    db.DB_PATH = db_path
    db._engine = None
    db._session_factory = None
    from backend.db import Base, get_engine

    Base.metadata.create_all(bind=get_engine())


def test_bundle_roundtrip_and_attachments(two_dbs):
    left, right = two_dbs
    _use(left)
    with db.session_scope() as session:
        fid = _add_friend(session)
        session.add(
            Message(
                id=str(uuid.uuid4()),
                friend_id=fid,
                role="user",
                content="你好",
                created_at=_now(),
            )
        )
        session.add(
            Memory(
                id=str(uuid.uuid4()),
                friend_id=fid,
                content="他喜欢猫",
                tag="normal",
                layer="long_term",
                importance=0.8,
                created_at=_now(),
                refreshed_at=_now(),
                updated_at=_now(),
            )
        )
        portrait = left / "portraits" / fid
        portrait.mkdir(parents=True)
        (portrait / "开心.png").write_bytes(b"fake-png")
        bundle = sync_service.export_bundle(session)
    assert bundle["app"] == "yumeFriend"
    assert bundle["tables"]["friends"][0]["name"] == "阿月"
    assert any(item["path"].endswith("开心.png") for item in bundle["attachments"])
    _use(right)
    with db.session_scope() as session:
        result = sync_service.merge_bundle(session, bundle)
        assert result["imported"]["friends"] == 1
        assert result["imported"]["messages"] == 1
        assert sync_service.device_id(session)
    # 重复合并不产生重复
    with db.session_scope() as session:
        result2 = sync_service.merge_bundle(session, bundle)
        assert result2["imported"]["friends"] == 0
        assert session.execute(select(Friend)).scalars().all()[0].name == "阿月"


def test_live2d_and_portrait_pack_files_are_sync_attachments(two_dbs):
    left, _right = two_dbs
    _use(left)
    live2d = left / "live2d" / "model_settings" / "m1.json"
    live2d.parent.mkdir(parents=True)
    live2d.write_text('{"display_name":"模型"}', encoding="utf-8")
    pack = left / "portrait_packs" / "p1" / "高兴.png"
    pack.parent.mkdir(parents=True)
    pack.write_bytes(b"fake-png")

    paths = {item["path"] for item in sync_service.attachment_manifest()}
    assert "live2d/model_settings/m1.json" in paths
    assert "portrait_packs/p1/高兴.png" in paths


def test_tombstone_deletes_remote_copy(two_dbs):
    left, right = two_dbs
    _use(left)
    with db.session_scope() as session:
        fid = _add_friend(session)
        bundle = sync_service.export_bundle(session)
    _use(right)
    with db.session_scope() as session:
        sync_service.merge_bundle(session, bundle)
        assert session.get(Friend, fid) is not None
        session.add(
            SyncTombstone(
                id=str(uuid.uuid4()),
                entity="friends",
                entity_id=fid,
                deleted_at=_now(),
                device_id="left",
            )
        )
        bundle2 = sync_service.export_bundle(session)
    # 模拟另一台设备收到墓碑
    _use(left)
    with db.session_scope() as session:
        sync_service.merge_bundle(session, bundle2)
        assert session.get(Friend, fid) is None


def test_settings_owner_syncs(two_dbs):
    left, right = two_dbs
    _use(left)
    with db.session_scope() as session:
        row = session.get(Settings, 1) or Settings(id=1, updated_at="2026-10-21T00:00:00+00:00")
        row.autonomy_owner_device_id = "phone-1"
        row.updated_at = "2099-01-01T00:00:00+00:00"
        session.add(row)
        bundle = sync_service.export_bundle(session)
    _use(right)
    with db.session_scope() as session:
        sync_service.merge_bundle(session, bundle)
        row = session.get(Settings, 1)
        assert row is not None
        assert row.autonomy_owner_device_id == "phone-1"


def test_llm_endpoint_settings_stay_local(two_dbs):
    left, right = two_dbs
    _use(left)
    with db.session_scope() as session:
        row = session.get(Settings, 1) or Settings(id=1)
        row.purposes = json.dumps(
            {
                "chat": {
                    "base_url": "https://pc.example/v1",
                    "model": "pc-model",
                    "api_key": "pc-key",
                }
            }
        )
        row.llm_bases = json.dumps(
            [
                {
                    "name": "pc",
                    "base_url": "https://pc.example/v1",
                    "model": "pc-model",
                    "api_key": "pc-key",
                }
            ]
        )
        row.updated_at = "2099-01-01T00:00:00+00:00"
        session.add(row)
        bundle = sync_service.export_bundle(session)
    exported = bundle["tables"]["settings"][0]
    assert "purposes" not in exported
    assert "llm_bases" not in exported

    # 模拟旧版本同步包仍带模型配置：合并时必须无视。
    exported["purposes"] = {
        "chat": {"base_url": "https://old.example/v1", "model": "old-model"}
    }
    exported["llm_bases"] = [
        {"name": "old", "base_url": "https://old.example/v1", "model": "old-model"}
    ]

    _use(right)
    with db.session_scope() as session:
        local = session.get(Settings, 1) or Settings(id=1)
        local.purposes = json.dumps(
            {
                "chat": {
                    "base_url": "https://phone.example/v1",
                    "model": "phone-model",
                    "api_key": "phone-key",
                }
            }
        )
        local.llm_bases = json.dumps(
            [
                {
                    "name": "phone",
                    "base_url": "https://phone.example/v1",
                    "model": "phone-model",
                    "api_key": "phone-key",
                }
            ]
        )
        local.updated_at = "2026-01-01T00:00:00+00:00"
        session.add(local)

    with db.session_scope() as session:
        sync_service.merge_bundle(session, bundle)
        row = session.get(Settings, 1)
        purposes = json.loads(row.purposes)
        bases = json.loads(row.llm_bases)
        assert purposes["chat"]["base_url"] == "https://phone.example/v1"
        assert purposes["chat"]["api_key"] == "phone-key"
        assert bases[0]["name"] == "phone"
        assert bases[0]["api_key"] == "phone-key"
