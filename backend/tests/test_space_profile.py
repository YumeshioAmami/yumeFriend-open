"""小世界反应档案：解析、默认回退和一次性生成端点。"""

from __future__ import annotations

import json
from datetime import datetime, timezone

from fastapi import FastAPI
from fastapi.testclient import TestClient

import backend.config as config
import backend.db as db
from backend.api import friends as friends_api
from backend.api.deps import get_provider_factory
from backend.models import Friend
from backend.services import space_profile_service


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def test_parse_space_profile_merges_defaults_and_cleans_lines():
    raw = """```json
    {"attention":{"curiosity":2,"patience":-1},
     "idle":[
       {"kind":"stretch","text":"伸了个懒腰"},
       {"kind":"tail","text":"玩自己的尾巴"},
       {"kind":"window","text":"趴在窗边看雨"}
     ],
     "notice":{"lines":["你来了。"],"expression":"surprised"},
     "call":{"lines":["嗯，我在。"],"expression":"happy"},
     "zones":{"tail":{"lines":["别碰尾巴！","\\n再来试一次"],"expression":"angry"}},
     "objects":{"mug":{"lines":["水要洒了。"],"expression":"surprised"}}}
    ```"""
    profile = space_profile_service.parse_profile(raw)
    assert profile is not None
    assert profile["version"] == 3
    assert profile["attention"]["curiosity"] == 1.0
    assert profile["attention"]["patience"] == 0.0
    assert profile["idle"] == [
        {"kind": "stretch", "text": "伸了个懒腰"},
        {"kind": "tail", "text": "玩自己的尾巴"},
        {"kind": "window", "text": "趴在窗边看雨"},
    ]
    assert profile["notice"]["lines"] == ["你来了。"]
    assert profile["notice"]["expression"] == "surprised"
    assert profile["call"]["lines"] == ["嗯，我在。"]
    assert profile["zones"]["tail"]["lines"] == ["别碰尾巴！", "再来试一次"]
    assert profile["zones"]["tail"]["expression"] == "angry"
    assert profile["zones"]["head"]["lines"]  # 缺的字段用默认档案补齐
    assert profile["objects"]["mug"] == {"lines": ["水要洒了。"], "expression": "surprised"}
    assert profile["objects"]["book"]["lines"]  # 缺的道具反应也补默认


def test_parse_space_profile_returns_none_for_non_json():
    assert space_profile_service.parse_profile("我只想说话，不想交 JSON") is None


def test_parse_legacy_string_idle_infers_visible_activity_kind():
    profile = space_profile_service.parse_profile(
        '{"idle":["低头玩自己的尾巴","拿着手机刷视频","窝在窗边看雨"]}'
    )
    assert profile is not None
    assert [item["kind"] for item in profile["idle"]] == ["tail", "phone", "window"]


def test_space_profile_endpoint_uses_llm_and_falls_back(tmp_path, monkeypatch):
    db_file = tmp_path / "space-profile.db"
    monkeypatch.setattr(config, "DB_PATH", db_file)
    monkeypatch.setattr(db, "DB_PATH", db_file)
    monkeypatch.setattr(db, "_engine", None)
    monkeypatch.setattr(db, "_session_factory", None)
    from backend.db import Base, get_engine

    Base.metadata.create_all(get_engine())
    with db.session_scope() as session:
        session.add(
            Friend(
                id="f-space",
                name="汐音",
                personality="嘴硬但心软，喜欢被摸头；尾巴被碰会先警告。",
                life_enabled=0,
                created_at=_now(),
                updated_at=_now(),
            )
        )

    class FakeProvider:
        async def chat_stream(self, messages, **kwargs):  # noqa: ANN001, ANN003
            yield {
                "type": "content",
                "delta": json.dumps(
                    {
                        "attention": {"curiosity": 0.8, "patience": 0.3, "initiative": 0.7},
                        "idle": [
                            {"kind": "window", "text": "在窗边看雨"},
                            {"kind": "read", "text": "翻一本旧书"},
                            {"kind": "phone", "text": "慢慢刷手机"},
                        ],
                        "notice": {"lines": ["你什么时候来的。"], "expression": "relaxed"},
                        "call": {"lines": ["我在呢。"], "expression": "happy"},
                        "zones": {
                            "head": {
                                "lines": ["唔，头发要乱了。"],
                                "expression": "shy",
                            }
                        },
                        "objects": {
                            "plush": {
                                "lines": ["别抢我的鲸鱼。"],
                                "expression": "happy",
                            }
                        },
                    },
                    ensure_ascii=False,
                ),
            }
            yield {"type": "done"}

        async def aclose(self) -> None:
            return None

    app = FastAPI()
    app.include_router(friends_api.router)
    app.dependency_overrides[get_provider_factory] = lambda: (
        lambda base_name=None: FakeProvider()
    )
    client = TestClient(app)
    response = client.post("/api/friends/f-space/space-profile")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["source"] == "llm"
    assert body["profile"]["zones"]["head"]["lines"] == ["唔，头发要乱了。"]
    assert body["profile"]["zones"]["head"]["expression"] == "shy"
    assert body["profile"]["idle"][0] == {"kind": "window", "text": "在窗边看雨"}
    assert body["profile"]["call"]["lines"] == ["我在呢。"]
    assert body["profile"]["objects"]["plush"]["lines"] == ["别抢我的鲸鱼。"]
