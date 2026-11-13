from __future__ import annotations

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from backend.db import Base
from backend.mobile_app import _ensure_missing_columns
from backend.models import Friend, Message, Settings


def test_mobile_upgrade_adds_missing_columns_without_losing_rows(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'mobile.db'}", future=True)
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        session.add(
            Settings(
                id=1,
                active_message_frequency=60,
                deep_thinking=0,
                show_tool_calls=0,
                llm_bases="[]",
                purposes="{}",
                updated_at="2026-11-14T00:00:00+00:00",
            )
        )
        session.add(
            Friend(
                id="friend-1",
                name="测试朋友",
                personality="",
                custom_instructions="",
                created_at="2026-11-14T00:00:00+00:00",
                updated_at="2026-11-14T00:00:00+00:00",
            )
        )
        session.add(
            Message(
                id="msg-1",
                friend_id="friend-1",
                role="user",
                content="旧消息",
                created_at="2026-11-14T00:00:00+00:00",
            )
        )
        session.commit()

    with engine.begin() as conn:
        conn.exec_driver_sql("ALTER TABLE friends DROP COLUMN visual_mode")
        conn.exec_driver_sql("ALTER TABLE messages DROP COLUMN segments")
        conn.exec_driver_sql("ALTER TABLE settings DROP COLUMN features")

    _ensure_missing_columns(engine)

    with Session(engine) as session:
        friend = session.scalar(select(Friend).where(Friend.id == "friend-1"))
        message = session.scalar(select(Message).where(Message.id == "msg-1"))
        settings = session.get(Settings, 1)

    assert friend is not None and friend.name == "测试朋友"
    assert friend.visual_mode == "live2d"
    assert message is not None and message.content == "旧消息"
    assert message.segments is None
    assert settings is not None and settings.features == "{}"
