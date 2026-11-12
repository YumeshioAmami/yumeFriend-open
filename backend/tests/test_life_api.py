"""v7.12 / D341：生活线 API 只读状态 + 开关。"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import backend.config as config
import backend.db as db
from backend.api import life as life_api
from backend.models import Friend
from backend.services import sync_service


@pytest.fixture()
def client(tmp_path, monkeypatch):
    db_file = tmp_path / "life-api.db"
    monkeypatch.setattr(config, "DB_PATH", db_file)
    monkeypatch.setattr(db, "DB_PATH", db_file)
    monkeypatch.setattr(db, "_engine", None)
    monkeypatch.setattr(db, "_session_factory", None)
    from backend.db import Base, get_engine

    Base.metadata.create_all(get_engine())
    now = datetime.now(timezone.utc).isoformat()
    with db.session_scope() as session:
        sync_service.configure_device(session, name="测试电脑", kind="desktop")
        session.add(
            Friend(
                id="f-life",
                name="汐音",
                life_enabled=0,
                created_at=now,
                updated_at=now,
            )
        )
    app = FastAPI()
    app.include_router(life_api.router)
    return TestClient(app)


def test_life_status_and_toggle(client):
    status = client.get("/api/life/f-life/status").json()
    assert status["enabled"] is False
    assert status["current_activity"] is None

    enabled = client.patch("/api/life/f-life", json={"enabled": True}).json()
    assert enabled["enabled"] is True
    assert enabled["recent"] == []
