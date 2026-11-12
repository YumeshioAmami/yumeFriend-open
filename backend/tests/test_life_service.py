"""v7.12 / D341：生活线真实活动、暂停、完成与清理。"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone

import pytest

import backend.config as config
import backend.db as db
from backend.models import Friend, FriendLife, LifeActivity, LifeProject
from backend.services import life_service, sync_service, web_search_service


@pytest.fixture()
def life_friend(tmp_path, monkeypatch):
    monkeypatch.setattr(life_service, "LIFE_ACTIVE_START_HOUR", 0)
    monkeypatch.setattr(life_service, "LIFE_ACTIVE_END_HOUR", 24)
    db_file = tmp_path / "life.db"
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
                life_enabled=1,
                created_at=now,
                updated_at=now,
            )
        )
    return "f-life"


def _fake_result() -> dict:
    return {
        "kind": "bilibili_ranking",
        "title": "刷 B 站热门",
        "summary": "刷了会儿 B 站热门，看到：测试视频",
        "observations": [
            {"title": "测试视频", "url": "https://www.bilibili.com/video/BVtest", "source": "bilibili"}
        ],
        "tool_calls": [
            {"tool": "bilibili_ranking", "url": "https://api.bilibili.com/x/web-interface/ranking/v2", "ok": True, "status": 200}
        ],
        "share_worthy": True,
        "project_topic": "测试视频",
        "project_source": "bilibili",
        "read_excerpt": "测试视频的简介",
        "content_depth": "metadata",
    }


def test_tick_runs_real_activity_and_records_evidence(life_friend, monkeypatch):
    monkeypatch.setattr(life_service, "_run_bilibili_ranking", lambda *_a, **_k: _fake_result())

    out = asyncio.run(life_service.tick())

    assert out["started"] == 1
    with db.session_scope() as session:
        life = session.get(FriendLife, life_friend)
        activity = session.get(LifeActivity, life.current_activity_id)
        assert activity is not None
        assert activity.status == "running"
        assert activity.summary.startswith("刷了会儿 B 站热门")
        assert json.loads(activity.tool_calls)[0]["ok"] is True
        assert json.loads(activity.observations)[0]["title"] == "测试视频"
        note = life_service.note_for_prompt(session, life_friend)
        assert "刷了会儿 B 站热门" in note
        assert "你自己的生活在进行" in note


def test_due_activity_completes_and_cooldown_starts(life_friend, monkeypatch):
    monkeypatch.setattr(life_service, "_run_bilibili_ranking", lambda *_a, **_k: _fake_result())
    asyncio.run(life_service.tick())

    with db.session_scope() as session:
        life = session.get(FriendLife, life_friend)
        activity = session.get(LifeActivity, life.current_activity_id)
        activity.ends_at = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()

    out = asyncio.run(life_service.tick())
    assert out["finished"] == 1
    with db.session_scope() as session:
        life = session.get(FriendLife, life_friend)
        assert life.current_activity_id is None
        assert life.last_activity_at
        assert "刷了会儿 B 站热门" in life.last_observation


def test_chat_pauses_new_activity(life_friend, monkeypatch):
    monkeypatch.setattr(life_service, "_run_bilibili_ranking", lambda *_a, **_k: _fake_result())
    life_service.chat_started(life_friend)
    out = asyncio.run(life_service.tick())
    assert out["started"] == 0
    with db.session_scope() as session:
        life = session.get(FriendLife, life_friend)
        assert life.paused_by_chat == 1


def test_disabling_life_cancels_current_activity(life_friend, monkeypatch):
    monkeypatch.setattr(life_service, "_run_bilibili_ranking", lambda *_a, **_k: _fake_result())
    asyncio.run(life_service.tick())
    with db.session_scope() as session:
        friend = session.get(Friend, life_friend)
        life = life_service.set_enabled(session, friend, False)
        assert life.current_activity_id is None
        activity = session.query(LifeActivity).filter_by(friend_id=life_friend).one()
        assert activity.status == "cancelled"


def test_failure_is_recorded_not_faked(life_friend, monkeypatch):
    def boom(*_args, **_kwargs):
        raise RuntimeError("网络断了")

    monkeypatch.setattr(life_service, "_run_bilibili_ranking", boom)
    asyncio.run(life_service.tick())
    with db.session_scope() as session:
        activity = session.query(LifeActivity).filter_by(friend_id=life_friend).one()
        assert activity.status == "failed"
        assert "网络断了" in activity.summary
        assert json.loads(activity.tool_calls)[0]["ok"] is False


def test_web_read_generic_homepages_are_not_share_worthy(monkeypatch):
    def no_feed(*_args, **_kwargs):
        raise RuntimeError("rss down")

    monkeypatch.setattr(life_service, "_run_feed_read", no_feed)

    def fake_search(query, *, proxy=""):
        return {
            "results": [
                {"title": "百度一下，你就知道", "url": "https://www.baidu.com", "snippet": ""},
                {"title": "哔哩哔哩下载中心", "url": "https://app.bilibili.com", "snippet": ""},
            ],
            "excerpts": [],
        }

    monkeypatch.setattr(web_search_service, "search", fake_search)
    result = life_service._run_web_read(())
    assert result["share_worthy"] is False
    assert "没翻到值得看的东西" in result["summary"]


def test_web_read_with_real_excerpt_is_share_worthy(monkeypatch):
    body = "研究人员今天公布了一项新发现，他们用更简单的方法解释了这个问题。" * 3

    def no_feed(*_args, **_kwargs):
        raise RuntimeError("rss down")

    monkeypatch.setattr(life_service, "_run_feed_read", no_feed)

    def fake_search(query, *, proxy=""):
        return {
            "results": [
                {
                    "title": "研究人员公布一项新发现",
                    "url": "https://www.ithome.com/article",
                    "snippet": "",
                }
            ],
            "excerpts": [{"url": "https://www.ithome.com/article", "text": body}],
        }

    monkeypatch.setattr(web_search_service, "search", fake_search)
    result = life_service._run_web_read(())
    assert result["share_worthy"] is True
    assert "研究人员公布一项新发现" in result["summary"]
    assert "新发现" in result["summary"]


def test_feed_read_uses_real_rss_items(monkeypatch):
    xml = """<?xml version="1.0" encoding="UTF-8"?>
    <rss><channel>
      <item>
        <title>一台三十年前的收音机被修好了</title>
        <link>https://example.com/radio</link>
        <description><![CDATA[修好之后，喇叭里先传出一阵电流声，然后才有了人声。]]></description>
      </item>
    </channel></rss>"""
    monkeypatch.setattr(life_service, "_http_text", lambda *_a, **_k: xml)
    result = life_service._run_feed_read(())
    assert result["share_worthy"] is True
    assert "三十年前的收音机" in result["summary"]
    assert result["observations"][0]["url"] == "https://example.com/radio"


def test_unworthy_activity_keeps_last_observation_but_no_pending_share(life_friend, monkeypatch):
    result = _fake_result()
    result["summary"] = "刷了会儿 B 站热门，都是之前看过的，没有新东西。"
    result["share_worthy"] = False
    monkeypatch.setattr(life_service, "_run_bilibili_ranking", lambda *_a, **_k: result)
    asyncio.run(life_service.tick())
    with db.session_scope() as session:
        life = session.get(FriendLife, life_friend)
        assert life.pending_share == ""
        assert "没有新东西" in life.last_observation


def test_bilibili_all_seen_is_not_share_worthy(monkeypatch):
    monkeypatch.setattr(
        life_service,
        "_http_json",
        lambda *_a, **_k: {
            "code": 0,
            "data": {
                "list": [
                    {"bvid": "BV1", "title": "一个看过的视频", "desc": ""},
                    {"bvid": "BV2", "title": "另一个看过的视频", "desc": ""},
                ]
            },
        },
    )
    result = life_service._run_bilibili_ranking(
        (), {"https://www.bilibili.com/video/BV1", "https://www.bilibili.com/video/BV2"}
    )
    assert result["share_worthy"] is False
    assert "没有新东西" in result["summary"]


def test_article_extractor_skips_navigation_and_scripts():
    article = "研究人员今天公布了一项新发现。" * 20
    html = f"""
    <html><body>
      <nav>首页 登录 下载中心</nav>
      <script>window.bad = true;</script>
      <article><p>{article}</p></article>
      <footer>版权所有</footer>
    </body></html>
    """
    text = life_service._extract_article_text(html)
    assert "研究人员今天公布了一项新发现" in text
    assert "首页" not in text
    assert "window.bad" not in text
    assert "版权所有" not in text


def test_followup_query_ignores_section_label_and_rejects_false_match():
    topic = "派早报：Apple 发布 iPad mini (A20 Pro) 等"
    query = life_service._followup_query(topic)
    terms = life_service._topic_terms(topic)
    assert "apple" in query.lower()
    assert "ipad" in query.lower()
    assert "派早报" not in query
    assert not life_service._matches_topic(
        "为什么我们数学老师把圆周率 π 读作「派」？ - 知乎",
        terms,
        topic,
    )


def test_feed_read_fetches_article_body_and_marks_full_text(monkeypatch):
    xml = """<?xml version="1.0" encoding="UTF-8"?>
    <rss><channel>
      <item>
        <title>一台三十年前的收音机被修好了</title>
        <link>https://example.com/radio</link>
        <description><![CDATA[简短摘要。]]></description>
      </item>
    </channel></rss>"""
    body = "修好之后，喇叭里先传出一阵电流声，然后才有人声。" * 30

    def fake_http(url, *, timeout=15.0):  # noqa: ANN001, ARG001
        if url == "https://example.com/radio":
            return f"<html><body><article><p>{body}</p></article></body></html>"
        return xml

    monkeypatch.setattr(life_service, "_http_text", fake_http)
    result = life_service._run_feed_read(())
    assert result["content_depth"] == "full_text"
    assert "修好之后" in result["read_excerpt"]
    assert len(result["read_excerpt"]) > 200


def test_tick_creates_life_project_and_links_activity(life_friend, monkeypatch):
    monkeypatch.setattr(life_service, "_run_bilibili_ranking", lambda *_a, **_k: _fake_result())
    asyncio.run(life_service.tick())
    with db.session_scope() as session:
        project = session.query(LifeProject).filter_by(friend_id=life_friend).one()
        activity = session.query(LifeActivity).filter_by(friend_id=life_friend).one()
        assert project.status == "active"
        assert project.step_count == 1
        assert project.topic == "测试视频"
        assert project.content_depth == "metadata"
        assert activity.project_id == project.id
        assert "只看到了标题和简介" in life_service.project_prompt_note(
            session, life_friend
        )


def test_followup_continues_project(life_friend, monkeypatch):
    now = datetime.now(timezone.utc)
    with db.session_scope() as session:
        life = life_service.get_or_create_life(session, life_friend)
        life.last_activity_at = (now - timedelta(minutes=10)).isoformat()
        session.add(
            LifeProject(
                id="p-follow",
                friend_id=life_friend,
                topic="一台三十年前的收音机被修好了",
                status="active",
                source="rss:少数派",
                step_count=1,
                reading_excerpt="上次读到的正文。",
                content_depth="full_text",
                last_thought="我上次说这声音很有意思。",
                open_question="它后来还能用吗？",
                evidence="[]",
                last_activity_at=(now - timedelta(hours=1)).isoformat(),
                created_at=(now - timedelta(hours=1)).isoformat(),
                updated_at=(now - timedelta(hours=1)).isoformat(),
            )
        )

    def fake_followup(project, *_args, **_kwargs):
        return {
            "kind": "project_followup",
            "title": "接着上次的收音机",
            "summary": "接着上次的「收音机」，又看到：修好之后声音更清楚了",
            "observations": [
                {
                    "title": "修好之后声音更清楚了",
                    "url": "https://example.com/radio-2",
                    "source": "project_followup",
                    "excerpt": "正文" * 200,
                }
            ],
            "tool_calls": [{"tool": "project_followup", "ok": True}],
            "share_worthy": True,
            "project_topic": project.topic,
            "project_source": "web",
            "read_excerpt": "新的正文" * 80,
            "content_depth": "full_text",
        }

    monkeypatch.setattr(life_service, "_run_project_followup", fake_followup)
    asyncio.run(life_service.tick())
    with db.session_scope() as session:
        project = session.get(LifeProject, "p-follow")
        activity = session.query(LifeActivity).filter_by(friend_id=life_friend).one()
        assert activity.kind == "project_followup"
        assert project.step_count == 2
        assert project.last_thought == "我上次说这声音很有意思。"
        assert project.open_question == "它后来还能用吗？"
        assert "新的正文" in project.reading_excerpt


def test_followup_without_new_content_rests_project(life_friend, monkeypatch):
    now = datetime.now(timezone.utc)
    with db.session_scope() as session:
        life = life_service.get_or_create_life(session, life_friend)
        life.last_activity_at = (now - timedelta(minutes=10)).isoformat()
        session.add(
            LifeProject(
                id="p-rest",
                friend_id=life_friend,
                topic="一个找不到后续的话题",
                status="active",
                source="rss:少数派",
                step_count=1,
                reading_excerpt="上次读到的正文。",
                content_depth="full_text",
                last_thought="",
                open_question="",
                evidence="[]",
                last_activity_at=(now - timedelta(hours=1)).isoformat(),
                created_at=(now - timedelta(hours=1)).isoformat(),
                updated_at=(now - timedelta(hours=1)).isoformat(),
            )
        )

    def fake_followup(project, *_args, **_kwargs):
        return {
            "kind": "project_followup",
            "title": "接着上次的话题",
            "summary": "接着上次的话题又找了找，暂时没看到新的东西。",
            "observations": [],
            "tool_calls": [{"tool": "project_followup", "ok": True, "fresh": 0}],
            "share_worthy": False,
            "project_topic": project.topic,
            "project_source": project.source,
            "read_excerpt": "",
            "content_depth": "none",
            "project_rest": True,
        }

    monkeypatch.setattr(life_service, "_run_project_followup", fake_followup)
    asyncio.run(life_service.tick())
    with db.session_scope() as session:
        project = session.get(LifeProject, "p-rest")
        life = session.get(FriendLife, life_friend)
        assert project.status == "resting"
        assert life.pending_share == ""


def test_record_project_thought_keeps_question_for_next_time(life_friend):
    with db.session_scope() as session:
        project = LifeProject(
            id="p-thought",
            friend_id=life_friend,
            topic="老式收音机修复",
            status="active",
            source="rss:少数派",
            step_count=1,
            reading_excerpt="正文",
            content_depth="full_text",
            last_thought="",
            open_question="",
            evidence="[]",
            last_activity_at=datetime.now(timezone.utc).isoformat(),
            created_at=datetime.now(timezone.utc).isoformat(),
            updated_at=datetime.now(timezone.utc).isoformat(),
        )
        session.add(project)
        life_service.record_project_thought(
            session, life_friend, "我后来又看到一段。它到底为什么还能修好？"
        )
        assert project.last_thought.startswith("我后来又看到")
        assert project.open_question.endswith("？")


def test_deleting_friend_cascades_life_projects(life_friend):
    with db.session_scope() as session:
        now = datetime.now(timezone.utc).isoformat()
        session.add(
            LifeProject(
                id="p-cascade",
                friend_id=life_friend,
                topic="会被一起删掉的话题",
                status="active",
                source="rss:少数派",
                step_count=1,
                reading_excerpt="正文",
                content_depth="full_text",
                last_thought="",
                open_question="",
                evidence="[]",
                last_activity_at=now,
                created_at=now,
                updated_at=now,
            )
        )
    with db.session_scope() as session:
        friend = session.get(Friend, life_friend)
        session.delete(friend)
    with db.session_scope() as session:
        assert session.query(LifeProject).count() == 0
