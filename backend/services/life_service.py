"""生活线（v7.12 / D341，v7.17 / D360）——电脑端唯一的真实活动循环。

这里不做“随机心情生成”。每次活动都必须真的调用外部工具（B 站公开接口 /
Bing 搜索 / RSS），并把实际返回的标题、来源、HTTP 状态写进活动记录。
从 v7.17 起，网页/RSS 活动会进一步抓取文章正文；有内容的线索会形成一个
最多三步的“生活项目”，下次优先接着读、接着想，而不是每次重新抽一条新闻。
手机端永远不调用 start/tick；它只通过同步数据查看电脑端的活动。
"""

from __future__ import annotations

import json
import random
import re
import uuid
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
from typing import Any

import httpx
from loguru import logger
from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.db import session_scope
from backend.features import events as feature_events
from backend.models import Friend, FriendLife, LifeActivity, LifeProject
from backend.services import web_search_service

LIFE_COOLDOWN_SECONDS = 90
CHAT_PAUSE_SECONDS = 180
ACTIVITY_MIN_SECONDS = 180
ACTIVITY_MAX_SECONDS = 420
LIFE_ACTIVE_START_HOUR = 8
LIFE_ACTIVE_END_HOUR = 23
LIFE_PROJECT_MAX_STEPS = 3
LIFE_PROJECT_CONTINUE_SECONDS = 36 * 3600
LIFE_PROJECT_PROMPT_SECONDS = 72 * 3600
ARTICLE_EXCERPT_LIMIT = 1800

_INTEREST_WORDS = (
    "B站", "动漫", "动画", "游戏", "科技", "电影", "音乐", "历史", "科普",
    "编程", "AI", "人工智能", "小说", "摄影", "美食", "旅行", "学习", "汽车",
    "体育", "数码", "鬼畜", "搞笑", "纪录片", "原神", "崩坏", "绝区零",
)

_GENERIC_WEB_TITLE_MARKERS = (
    "百度一下",
    "bilibili",
    "哔哩哔哩下载",
    "哔哩哔哩 -",
    "首页",
    "登录",
    "下载中心",
    "官方网站",
)

_JUNK_WEB_MARKERS = (
    "黄历",
    "日历",
    "天气预报",
    "浏览器环境",
    "正在验证",
    "百度百科",
    "汉语字典",
    "翻译",
)

_TRUSTED_CONTENT_DOMAINS = (
    "ithome.com",
    "sspai.com",
    "solidot.org",
    "36kr.com",
    "thepaper.cn",
    "github.com",
    "arxiv.org",
    "sciencedaily.com",
    "newscientist.com",
    "zhihu.com",
)

_LATIN_TERM_RE = re.compile(r"[A-Za-z][A-Za-z0-9+.-]{1,}")
_NUMBER_TERM_RE = re.compile(r"\d{2,}")
_CHINESE_TERM_RE = re.compile(r"[\u4e00-\u9fff]{2,}")
_SECTION_LABELS = ("早报", "晚报", "日报", "周报", "快讯", "简报", "要闻")
_FOLLOWUP_STOP_TERMS = {
    "发布",
    "推出",
    "宣布",
    "消息",
    "新闻",
    "最新",
    "进展",
    "更新",
    "今天",
    "昨天",
    "我们",
    "他们",
    "这个",
    "那个",
    "什么",
}

_RSS_FEEDS = (
    ("IT之家", "https://www.ithome.com/rss/"),
    ("少数派", "https://sspai.com/feed"),
    ("Solidot", "https://www.solidot.org/index.rss"),
)

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime | None = None) -> str:
    return (value or _now()).isoformat()


def _parse(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _loads(raw: str | None, default: Any) -> Any:
    try:
        value = json.loads(raw or "")
    except (TypeError, ValueError):
        return default
    return value if isinstance(value, type(default)) else default


def get_or_create_life(session: Session, friend_id: str) -> FriendLife:
    life = session.get(FriendLife, friend_id)
    if life is not None:
        return life
    if session.get(Friend, friend_id) is None:
        raise ValueError(f"friend not found: {friend_id}")
    life = FriendLife(
        friend_id=friend_id,
        current_activity_id=None,
        last_activity_at=None,
        last_observation="",
        pending_share="",
        last_share_at=None,
        paused_by_chat=0,
        paused_until=None,
        updated_at=_iso(),
    )
    session.add(life)
    session.flush()
    return life


def set_enabled(session: Session, friend: Friend, enabled: bool) -> FriendLife:
    friend.life_enabled = 1 if enabled else 0
    friend.updated_at = _iso()
    life = get_or_create_life(session, friend.id)
    if not enabled and life.current_activity_id:
        current = session.get(LifeActivity, life.current_activity_id)
        if current is not None and current.status == "running":
            current.status = "cancelled"
            current.finished_at = _iso()
            current.summary = "生活线被关闭，这次活动取消"
            current.updated_at = _iso()
        life.current_activity_id = None
    life.updated_at = _iso()
    session.flush()
    return life


def chat_started(friend_id: str) -> None:
    """用户消息最高优先级：聊天开始 → 暂停发起新的生活决策（D341.5）。"""
    try:
        with session_scope() as session:
            now = _now()
            if _device_is_life_host(session):
                life = get_or_create_life(session, friend_id)
                life.paused_by_chat = 1
                life.paused_until = _iso(now + timedelta(seconds=CHAT_PAUSE_SECONDS))
                life.updated_at = _iso(now)
            else:
                # 手机没有生活循环；只把暂停窗口写给电脑，不碰 current_activity_id。
                friend = session.get(Friend, friend_id)
                if friend is not None:
                    friend.chat_pause_until = _iso(
                        now + timedelta(seconds=CHAT_PAUSE_SECONDS)
                    )
                    friend.updated_at = _iso(now)
    except Exception as exc:  # noqa: BLE001 —— 生活线不能拖垮聊天
        logger.warning("生活线暂停失败（不影响聊天）：{}", exc)


def chat_finished(friend_id: str) -> None:
    """聊天停歇后给生活线一个短冷却，避免用户刚说完她立刻切走。"""
    try:
        with session_scope() as session:
            now = _now()
            if _device_is_life_host(session):
                life = get_or_create_life(session, friend_id)
                life.paused_by_chat = 0
                life.paused_until = _iso(now + timedelta(seconds=60))
                life.updated_at = _iso(now)
            else:
                friend = session.get(Friend, friend_id)
                if friend is not None:
                    friend.chat_pause_until = _iso(now + timedelta(seconds=60))
                    friend.updated_at = _iso(now)
    except Exception as exc:  # noqa: BLE001
        logger.warning("生活线恢复失败（不影响聊天）：{}", exc)


def _device_is_life_host(session: Session) -> bool:
    try:
        from backend.services import sync_service  # noqa: PLC0415

        return sync_service.device_kind(session) == "desktop"
    except Exception:  # noqa: BLE001
        return False


def _http_json(url: str, *, params: dict | None = None, timeout: float = 15.0) -> dict:
    with httpx.Client(
        timeout=httpx.Timeout(timeout, connect=8.0),
        follow_redirects=True,
        headers=_HEADERS,
    ) as client:
        response = client.get(url, params=params)
    if response.status_code >= 400:
        raise RuntimeError(f"HTTP {response.status_code}")
    payload = response.json()
    if not isinstance(payload, dict):
        raise RuntimeError("返回不是 JSON 对象")
    return payload


def _http_text(url: str, *, timeout: float = 15.0) -> str:
    with httpx.Client(
        timeout=httpx.Timeout(timeout, connect=8.0),
        follow_redirects=True,
        headers=_HEADERS,
    ) as client:
        response = client.get(url)
    if response.status_code >= 400:
        raise RuntimeError(f"HTTP {response.status_code}")
    return response.text


def _recent_urls(session: Session, friend_id: str, *, limit: int = 12) -> set[str]:
    rows = session.execute(
        select(LifeActivity)
        .where(LifeActivity.friend_id == friend_id)
        .order_by(LifeActivity.started_at.desc())
        .limit(limit)
    ).scalars()
    urls: set[str] = set()
    for activity in rows:
        for item in _loads(activity.observations, []):
            if isinstance(item, dict) and item.get("url"):
                urls.add(str(item["url"]))
    return urls


def _interest_keywords(session: Session, friend: Friend) -> tuple[str, ...]:
    """从人格 + 最近记忆里捞兴趣词；查不到就用空（不硬猜）。"""
    text = " ".join(
        [
            friend.personality or "",
            friend.custom_instructions or "",
            friend.voice_examples or "",
        ]
    )
    try:
        from backend.services import memory_service  # noqa: PLC0415

        for memory in memory_service.list_alive(session, friend.id)[:80]:
            text += " " + (memory.content or "")
    except Exception:  # noqa: BLE001
        pass
    found = [word for word in _INTEREST_WORDS if word.lower() in text.lower()]
    return tuple(found[:4])


def latest_project(session: Session, friend_id: str) -> LifeProject | None:
    return session.execute(
        select(LifeProject)
        .where(LifeProject.friend_id == friend_id)
        .order_by(LifeProject.last_activity_at.desc(), LifeProject.created_at.desc())
        .limit(1)
    ).scalar_one_or_none()


def active_project(session: Session, friend_id: str) -> LifeProject | None:
    project = session.execute(
        select(LifeProject)
        .where(
            LifeProject.friend_id == friend_id,
            LifeProject.status == "active",
        )
        .order_by(LifeProject.last_activity_at.desc(), LifeProject.created_at.desc())
        .limit(1)
    ).scalar_one_or_none()
    if project is None:
        return None
    touched = _parse(project.last_activity_at) or _parse(project.created_at)
    if touched is not None and (_now() - touched).total_seconds() > LIFE_PROJECT_CONTINUE_SECONDS:
        return None
    return project


def _project_topic(result: dict[str, Any], activity: LifeActivity) -> str:
    topic = str(result.get("project_topic") or "").strip()
    if not topic:
        observations = result.get("observations") or []
        if observations and isinstance(observations[0], dict):
            topic = str(observations[0].get("title") or "").strip()
    if not topic:
        topic = (activity.title or "").strip()
    return re.sub(r"\s+", " ", topic)[:160]


def _merge_project_evidence(project: LifeProject, observations: Any) -> None:
    existing = _loads(project.evidence, [])
    if not isinstance(existing, list):
        existing = []
    for item in observations if isinstance(observations, list) else []:
        if not isinstance(item, dict):
            continue
        key = str(item.get("url") or item.get("title") or "").strip()
        if not key:
            continue
        if any(
            isinstance(old, dict)
            and key == str(old.get("url") or old.get("title") or "").strip()
            for old in existing
        ):
            continue
        existing.append(item)
    project.evidence = json.dumps(existing[-12:], ensure_ascii=False)


def _create_project(
    session: Session,
    activity: LifeActivity,
    result: dict[str, Any],
) -> LifeProject | None:
    topic = _project_topic(result, activity)
    if not topic:
        return None
    now = _now()
    for old in session.execute(
        select(LifeProject).where(
            LifeProject.friend_id == activity.friend_id,
            LifeProject.status == "active",
        )
    ).scalars():
        old.status = "resting"
        old.updated_at = _iso(now)
    project = LifeProject(
        id=str(uuid.uuid4()),
        friend_id=activity.friend_id,
        topic=topic,
        status="active",
        source=str(result.get("project_source") or ""),
        step_count=1,
        reading_excerpt=str(result.get("read_excerpt") or "")[:ARTICLE_EXCERPT_LIMIT],
        content_depth=str(result.get("content_depth") or ""),
        last_thought="",
        open_question="",
        evidence="[]",
        last_activity_at=_iso(now),
        last_shared_at=None,
        created_at=_iso(now),
        updated_at=_iso(now),
    )
    _merge_project_evidence(project, result.get("observations"))
    session.add(project)
    session.flush()
    return project


def _continue_project(
    session: Session,
    project: LifeProject,
    result: dict[str, Any],
) -> None:
    now = _now()
    project.last_activity_at = _iso(now)
    project.updated_at = _iso(now)
    if bool(result.get("project_rest")):
        project.status = "resting"
        session.flush()
        return
    project.step_count = int(project.step_count or 0) + 1
    project.source = str(result.get("project_source") or project.source or "")
    excerpt = str(result.get("read_excerpt") or "").strip()
    if excerpt:
        project.reading_excerpt = excerpt[:ARTICLE_EXCERPT_LIMIT]
        project.content_depth = str(result.get("content_depth") or project.content_depth or "")
    _merge_project_evidence(project, result.get("observations"))
    if project.step_count >= LIFE_PROJECT_MAX_STEPS:
        project.status = "completed"
    session.flush()


def _extract_open_question(text: str) -> str:
    for sentence in re.split(r"(?<=[。！？!?])", text or ""):
        cleaned = sentence.strip()
        if cleaned and ("?" in cleaned or "？" in cleaned):
            tail = re.split(r"——|—|：|:", cleaned)[-1].strip()
            if tail and ("?" in tail or "？" in tail):
                cleaned = tail
            return cleaned[:220]
    return ""


def record_project_thought(session: Session, friend_id: str, text: str) -> LifeProject | None:
    """把她真正说出口的那段话记成这次生活项目的想法；问题留作下次的线索。"""
    project = active_project(session, friend_id) or latest_project(session, friend_id)
    if project is None:
        return None
    spoken = re.sub(r"\s+", " ", (text or "").strip())[:1200]
    if not spoken:
        return project
    now = _iso()
    project.last_thought = spoken
    question = _extract_open_question(spoken)
    if question:
        project.open_question = question
    project.last_shared_at = now
    project.updated_at = now
    session.flush()
    return project


def project_to_dict(project: LifeProject) -> dict[str, Any]:
    return {
        "id": project.id,
        "friend_id": project.friend_id,
        "topic": project.topic,
        "status": project.status,
        "source": project.source,
        "step_count": int(project.step_count or 0),
        "reading_excerpt": project.reading_excerpt,
        "content_depth": project.content_depth,
        "last_thought": project.last_thought,
        "open_question": project.open_question,
        "evidence": _loads(project.evidence, []),
        "last_activity_at": project.last_activity_at,
        "last_shared_at": project.last_shared_at,
        "created_at": project.created_at,
        "updated_at": project.updated_at,
    }


def project_prompt_note(session: Session, friend_id: str) -> str:
    """给聊天 / 主动消息注入“这条生活线已经进行到哪一步”。"""
    project = latest_project(session, friend_id)
    if project is None:
        return ""
    touched = _parse(project.last_activity_at) or _parse(project.created_at)
    if touched is not None and (_now() - touched).total_seconds() > LIFE_PROJECT_PROMPT_SECONDS:
        return ""
    lines = ["[你自己的生活项目]"]
    if int(project.step_count or 0) > 1:
        lines.append(f"你最近在追一个话题：{project.topic}（已经追了 {project.step_count} 步）")
    else:
        lines.append(f"你最近开始在意一个话题：{project.topic}")
    excerpt = re.sub(r"\s+", " ", project.reading_excerpt or "").strip()
    if excerpt:
        lines.append(f"你读到的内容：{excerpt[:360]}")
    if project.last_thought:
        lines.append(f"你上次说过/想过：{project.last_thought[:260]}")
    if project.open_question:
        lines.append(f"你还没弄明白：{project.open_question}")
    if project.content_depth == "metadata":
        lines.append("注意：这次你只看到了标题和简介，没看视频画面、没听声音；别假装自己看了视频。")
    elif project.content_depth == "full_text":
        lines.append("这次你读的是文章正文，不是只看了标题。")
    lines.append("如果 TA 问起，就自然接着上次说；不要把自己做过的事说成第一次。")
    return "\n".join(lines)


def _run_bilibili_ranking(
    interests: tuple[str, ...] = (),
    seen_urls: set[str] | None = None,
) -> dict[str, Any]:
    url = "https://api.bilibili.com/x/web-interface/ranking/v2"
    payload = _http_json(url, params={"rid": 0, "type": "all"})
    if int(payload.get("code") or 0) != 0:
        raise RuntimeError(f"B站接口返回 code={payload.get('code')}")
    items = ((payload.get("data") or {}).get("list") or [])[:20]
    observations: list[dict[str, Any]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        bvid = str(item.get("bvid") or "")
        title = str(item.get("title") or "").strip()
        if not title:
            continue
        description = str(item.get("desc") or "")
        observations.append(
            {
                "title": title,
                "url": f"https://www.bilibili.com/video/{bvid}" if bvid else "",
                "source": "bilibili",
                "author": str((item.get("owner") or {}).get("name") or ""),
                "description": description[:120],
            }
        )
    if not observations:
        raise RuntimeError("B站接口没有返回视频")
    if interests:
        matched = [
            item
            for item in observations
            if any(word.lower() in (item["title"] + item.get("description", "")).lower() for word in interests)
        ]
        if matched:
            observations = matched
    seen = seen_urls or set()
    fresh = [item for item in observations if item.get("url") not in seen]
    if not fresh:
        return {
            "kind": "bilibili_ranking",
            "title": "刷 B 站热门",
            "summary": "刷了会儿 B 站热门，都是之前看过的，没有新东西。",
            "observations": observations[:5],
            "tool_calls": [
                {
                    "tool": "bilibili_ranking",
                    "url": f"{url}?rid=0&type=all",
                    "ok": True,
                    "status": 200,
                    "count": len(observations),
                    "fresh": 0,
                }
            ],
            "share_worthy": False,
            "project_topic": "",
            "project_source": "bilibili",
            "read_excerpt": "",
            "content_depth": "metadata",
        }
    observations = fresh[:5]
    titles = "；".join(item["title"] for item in observations[:3])
    interest_text = f"（最近在意：{'、'.join(interests)}）" if interests else ""
    return {
        "kind": "bilibili_ranking",
        "title": f"刷 B 站热门{interest_text}",
        "summary": f"刷了会儿 B 站热门{interest_text}，看到：{titles}",
        "observations": observations,
        "tool_calls": [
            {
                "tool": "bilibili_ranking",
                "url": f"{url}?rid=0&type=all",
                "ok": True,
                "status": 200,
                "count": len(observations),
                "fresh": len(fresh),
            }
        ],
        "share_worthy": True,
        "project_topic": observations[0]["title"],
        "project_source": "bilibili",
        "read_excerpt": (
            str(observations[0].get("description") or "").strip()
            or str(observations[0].get("title") or "").strip()
        ),
        "content_depth": "metadata",
    }


def _web_item_is_meaningful(item: dict[str, Any]) -> bool:
    """网页搜索结果里，至少要有真实内容，不能只有门户标题。"""
    title = str(item.get("title") or "").strip()
    snippet = str(item.get("snippet") or "").strip()
    excerpt = str(item.get("excerpt") or "").strip()
    haystack = f"{title} {snippet} {excerpt}"
    if any(marker in haystack for marker in _JUNK_WEB_MARKERS):
        return False
    if excerpt and len(excerpt) >= 80:
        return True
    if snippet and len(snippet) >= 35:
        return True
    lowered = title.lower()
    if any(marker in lowered for marker in _GENERIC_WEB_TITLE_MARKERS):
        return False
    return len(title) >= 18


def _url_is_trusted_content(url: str) -> bool:
    lowered = (url or "").lower()
    return any(domain in lowered for domain in _TRUSTED_CONTENT_DOMAINS)


def _strip_html_text(raw: str) -> str:
    text = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", raw or "")
    text = re.sub(r"(?s)<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", text).strip()


_HTML_SKIP_TAGS = {
    "script",
    "style",
    "noscript",
    "svg",
    "nav",
    "header",
    "footer",
    "aside",
    "form",
    "button",
    "iframe",
}
_HTML_BLOCK_TAGS = {
    "p",
    "div",
    "br",
    "li",
    "h1",
    "h2",
    "h3",
    "h4",
    "blockquote",
    "section",
    "tr",
}
_ARTICLE_JUNK_MARKERS = (
    "本站提到的所有注册商标",
    "版权所有",
    "备案号",
    "举报电话",
    "涉未成年人举报",
)


class _ReadableHTMLParser(HTMLParser):
    """极简正文提取：优先 article/main，剔除导航、脚本和表单等噪声。"""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._skip_depth = 0
        self._article_depth = 0
        self._body_parts: list[str] = []
        self._article_parts: list[str] = []
        self._paragraphs: list[str] = []
        self._current_paragraph: list[str] | None = None
        self._target = self._body_parts

    def handle_starttag(self, tag: str, _attrs) -> None:  # noqa: ANN001
        tag = tag.lower()
        if tag in _HTML_SKIP_TAGS:
            self._skip_depth += 1
            return
        if self._skip_depth:
            return
        if tag in ("article", "main"):
            self._article_depth += 1
            if self._article_depth == 1:
                self._target = self._article_parts
        if tag == "p":
            self._current_paragraph = []
        if tag in _HTML_BLOCK_TAGS:
            self._target.append("\n")

    def handle_startendtag(self, tag: str, attrs) -> None:  # noqa: ANN001
        self.handle_starttag(tag, attrs)
        self.handle_endtag(tag)

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag in _HTML_SKIP_TAGS:
            self._skip_depth = max(0, self._skip_depth - 1)
            return
        if self._skip_depth:
            return
        if tag in ("article", "main") and self._article_depth:
            self._article_depth -= 1
            if self._article_depth == 0:
                self._target = self._body_parts
                self._body_parts.append("\n")
        if tag == "p" and self._current_paragraph is not None:
            paragraph = re.sub(r"\s+", " ", "".join(self._current_paragraph)).strip()
            if len(paragraph) >= 12:
                self._paragraphs.append(paragraph)
            self._current_paragraph = None
        if tag in _HTML_BLOCK_TAGS:
            self._target.append("\n")

    def handle_data(self, data: str) -> None:
        if not self._skip_depth and data:
            self._target.append(data)
            if self._current_paragraph is not None:
                self._current_paragraph.append(data)


def _extract_article_text(raw_html: str, *, limit: int = ARTICLE_EXCERPT_LIMIT) -> str:
    """从网页 HTML 里抽可读正文；抽不到就返回空，绝不把导航当“读过的内容”。"""
    parser = _ReadableHTMLParser()
    try:
        parser.feed(raw_html or "")
        parser.close()
    except Exception:  # noqa: BLE001 —— 坏 HTML 不该让活动失败
        return ""
    article = "".join(parser._article_parts)  # noqa: SLF001 —— 局部解析器
    body = "".join(parser._body_parts)  # noqa: SLF001
    paragraph_list = [
        item
        for item in parser._paragraphs  # noqa: SLF001
        if not re.match(r"^\d+\s*分钟阅读", item)
    ]
    body_start = next(
        (
            index
            for index, item in enumerate(paragraph_list)
            if len(item) >= 30 and ("。" in item or "，" in item)
        ),
        0,
    )
    paragraphs = " ".join(paragraph_list[body_start:])
    paragraphs_are_usable = (
        len(paragraphs.strip()) >= 200
        and not any(marker in paragraphs[:600] for marker in _ARTICLE_JUNK_MARKERS)
    )
    if len(article.strip()) >= 200:
        text = article
    elif paragraphs_are_usable:
        text = paragraphs
    else:
        text = body
    for marker in _ARTICLE_JUNK_MARKERS:
        cut = text.find(marker)
        if cut > 200:
            text = text[:cut]
            break
    lines = []
    for line in re.split(r"[\r\n]+", text):
        cleaned = re.sub(r"[ \t\f\v]+", " ", line).strip()
        if len(cleaned) >= 6:
            lines.append(cleaned)
    normalized = re.sub(r"\s+", " ", " ".join(lines)).strip()
    return normalized[:limit]


def _fetch_article_text(url: str) -> str:
    if not url or not url.startswith(("http://", "https://")):
        return ""
    try:
        return _extract_article_text(_http_text(url, timeout=12.0))
    except Exception as exc:  # noqa: BLE001 —— 正文抓不到时退回摘要
        logger.info("正文抓取失败，退回摘要（{}）：{}", url[:80], exc)
        return ""


def _parse_rss_items(xml_text: str, source: str) -> list[dict[str, Any]]:
    """解析 RSS 2.0 的 item；只收标题、链接、描述，不把整篇网页塞进 DB。"""
    import xml.etree.ElementTree as ET

    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return []
    items: list[dict[str, Any]] = []
    for item in root.findall(".//item"):
        title = _strip_html_text(item.findtext("title") or "")
        link = (item.findtext("link") or "").strip()
        description = _strip_html_text(
            item.findtext("description")
            or item.findtext("{http://purl.org/rss/1.0/modules/content/}encoded")
            or ""
        )
        if not title:
            continue
        items.append(
            {
                "title": title,
                "url": link,
                "source": source,
                "snippet": description[:240],
            }
        )
        if len(items) >= 20:
            break
    return items


def _run_feed_read(
    interests: tuple[str, ...] = (),
    *,
    project: LifeProject | None = None,
) -> dict[str, Any]:
    """从真实 RSS 源读资讯，并尽量抓取第一篇的正文；比 Bing 结果页稳定。"""
    last_error: Exception | None = None
    feeds = list(_RSS_FEEDS)
    preferred_source = ""
    if project is not None and project.source.startswith("rss:"):
        preferred_source = project.source.split(":", 1)[1].strip()
    if preferred_source:
        feeds.sort(key=lambda item: 0 if item[0] == preferred_source else 1)
    else:
        random.shuffle(feeds)
    for source, url in feeds:
        try:
            items = _parse_rss_items(_http_text(url), source)
        except Exception as exc:  # noqa: BLE001 —— 这个源失败就换下一个
            last_error = exc
            continue
        if not items:
            last_error = RuntimeError(f"{source} RSS 没有条目")
            continue
        if interests:
            matched = [
                item
                for item in items
                if any(
                    word.lower() in f"{item['title']} {item.get('snippet', '')}".lower()
                    for word in interests
                )
            ]
            if matched:
                items = matched
        observations = items[:5]
        top = observations[0]
        article_text = _fetch_article_text(str(top.get("url") or ""))
        content_depth = "full_text" if len(article_text) >= 200 else "snippet"
        read_excerpt = article_text or str(top.get("snippet") or "")
        top_title = str(top.get("title") or "").strip()
        if top_title and read_excerpt.startswith(top_title):
            read_excerpt = read_excerpt[len(top_title):].lstrip(" |-—:：")
        details = []
        for item in observations[:3]:
            snippet = str(item.get("snippet") or "").strip()
            details.append(item["title"] + (f"：{snippet[:100]}" if snippet else ""))
        lead = re.sub(r"\s+", " ", read_excerpt).strip()[:180]
        return {
            "kind": "web_read",
            "title": f"读资讯：{source}",
            "summary": (
                f"读了「{source}」的《{top['title']}》"
                + (f"：{lead}" if lead else "")
                + (f"（另外还看到：{'；'.join(details[1:])}）" if len(details) > 1 else "")
            )[:420],
            "observations": observations,
            "tool_calls": [
                {
                    "tool": "rss_feed",
                    "source": source,
                    "url": url,
                    "ok": True,
                    "count": len(observations),
                    "article_url": top.get("url") or "",
                    "content_depth": content_depth,
                }
            ],
            "share_worthy": True,
            "project_topic": top_title,
            "project_source": f"rss:{source}",
            "read_excerpt": read_excerpt[:ARTICLE_EXCERPT_LIMIT],
            "content_depth": content_depth,
        }
    raise RuntimeError(f"RSS 都读不到：{last_error or '未知原因'}")


def _run_web_read(
    interests: tuple[str, ...] = (),
    *,
    project: LifeProject | None = None,
) -> dict[str, Any]:
    try:
        return _run_feed_read(interests, project=project)
    except Exception as exc:  # noqa: BLE001 —— RSS 失败才退回搜索
        logger.warning("RSS 资讯读取失败，退回搜索：{}", exc)

    queries = [
        "今天值得关注的科技新闻",
        "最近有意思的科技进展",
        "最近值得看的深度文章",
        "最近有什么有趣的发现",
    ]
    query = random.choice(queries)
    if interests:
        query = f"{query} {random.choice(interests)}"
    payload = web_search_service.search(query, proxy="")
    results = payload.get("results") or []
    excerpts = {
        str(item.get("url") or ""): str(item.get("text") or "").strip()[:400]
        for item in (payload.get("excerpts") or [])
        if isinstance(item, dict) and item.get("url") and item.get("text")
    }
    observations: list[dict[str, Any]] = []
    for item in results[:5]:
        if not isinstance(item, dict):
            continue
        title = str(item.get("title") or "").strip()
        url = str(item.get("url") or "").strip()
        if not title:
            continue
        observations.append(
            {
                "title": title,
                "url": url,
                "source": "web",
                "snippet": str(item.get("snippet") or "").strip(),
                "excerpt": excerpts.get(url, ""),
            }
        )
    if not observations:
        raise RuntimeError("搜索没有返回结果")

    meaningful = [
        item
        for item in observations
        if _url_is_trusted_content(str(item.get("url") or ""))
        and _web_item_is_meaningful(item)
    ]
    if not meaningful:
        return {
            "kind": "web_read",
            "title": f"读网页：{query}",
            "summary": f"搜了「{query}」，没翻到值得看的东西。",
            "observations": observations,
            "tool_calls": [
                {
                    "tool": "web_search",
                    "query": query,
                    "ok": True,
                    "status": 200,
                    "count": len(observations),
                    "meaningful": 0,
                }
            ],
            "share_worthy": False,
            "project_topic": "",
            "project_source": "web",
            "read_excerpt": "",
            "content_depth": "none",
        }

    details: list[str] = []
    for item in meaningful[:3]:
        detail = str(item.get("excerpt") or item.get("snippet") or "").strip()
        details.append(item["title"] + (f"：{detail[:100]}" if detail else ""))
    top = meaningful[0]
    read_excerpt = str(top.get("excerpt") or top.get("snippet") or "").strip()
    content_depth = "full_text" if len(read_excerpt) >= 200 else "snippet"
    return {
        "kind": "web_read",
        "title": f"读网页：{query}",
        "summary": (f"搜了「{query}」，看到：" + "；".join(details))[:360],
        "observations": observations,
        "tool_calls": [
            {
                "tool": "web_search",
                "query": query,
                "ok": True,
                "status": 200,
                "count": len(observations),
                "meaningful": len(meaningful),
            }
        ],
        "share_worthy": True,
        "project_topic": str(top.get("title") or "").strip(),
        "project_source": "web",
        "read_excerpt": read_excerpt[:ARTICLE_EXCERPT_LIMIT],
        "content_depth": content_depth,
    }


def _topic_terms(text: str) -> list[str]:
    raw = text or ""
    terms: list[str] = []
    for match in _LATIN_TERM_RE.findall(raw):
        term = match.lower().strip(".-")
        if len(term) >= 2 and term not in terms:
            terms.append(term)
    for match in _NUMBER_TERM_RE.findall(raw):
        if match not in terms:
            terms.append(match)
    for match in _CHINESE_TERM_RE.findall(raw):
        term = match.strip()
        if len(term) >= 2 and term not in _FOLLOWUP_STOP_TERMS and term not in terms:
            terms.append(term)
    return terms


def _followup_query(topic: str) -> str:
    cleaned = re.sub(r"\s+", " ", topic or "").strip()
    if "：" in cleaned:
        prefix, rest = cleaned.split("：", 1)
        if any(label in prefix for label in _SECTION_LABELS) and len(prefix) <= 12:
            cleaned = rest.strip()
    terms = _topic_terms(cleaned)
    query = " ".join(terms[:5]) if terms else cleaned[:36]
    return f"{query} 最新进展"


def _matches_topic(text: str, terms: list[str], topic: str) -> bool:
    haystack = (text or "").lower()
    if not haystack:
        return False
    if topic and topic.lower() in haystack:
        return True
    matched: set[str] = set()
    for term in terms:
        if term in haystack:
            matched.add(term)
            continue
        if re.fullmatch(r"[\u4e00-\u9fff]{4,}", term):
            shingles = {term[index:index + 2] for index in range(len(term) - 1)}
            threshold = 2 if len(term) <= 5 else 3
            if sum(1 for shingle in shingles if shingle in haystack) >= threshold:
                matched.add(term)
    needed = 1 if len(terms) <= 1 else 2
    return len(matched) >= needed


def _run_project_followup(
    project: LifeProject,
    interests: tuple[str, ...] = (),
    seen_urls: set[str] | None = None,
) -> dict[str, Any]:
    """接着上次的话题，用真实搜索找新进展；找不到就诚实休息，不硬编。"""
    topic = re.sub(r"\s+", " ", project.topic or "").strip()
    query = _followup_query(topic)
    seen = seen_urls or set()
    try:
        payload = web_search_service.search(query, proxy="")
    except Exception as exc:  # noqa: BLE001 —— 找不到也算一次真实尝试
        return {
            "kind": "project_followup",
            "title": f"接着上次的「{topic}」",
            "summary": f"接着上次的「{topic}」又找了找，搜索没成功：{exc}",
            "observations": [],
            "tool_calls": [
                {"tool": "project_followup", "query": query, "ok": False, "error": str(exc)[:300]}
            ],
            "share_worthy": False,
            "project_topic": topic,
            "project_source": project.source,
            "read_excerpt": "",
            "content_depth": "none",
            "project_rest": True,
        }
    results = payload.get("results") or []
    excerpts = {
        str(item.get("url") or ""): str(item.get("text") or "").strip()[:1000]
        for item in (payload.get("excerpts") or [])
        if isinstance(item, dict) and item.get("url") and item.get("text")
    }
    observations: list[dict[str, Any]] = []
    for item in results[:8]:
        if not isinstance(item, dict):
            continue
        title = str(item.get("title") or "").strip()
        url = str(item.get("url") or "").strip()
        if not title or not url or url in seen:
            continue
        observation = {
            "title": title,
            "url": url,
            "source": "project_followup",
            "snippet": str(item.get("snippet") or "").strip(),
            "excerpt": excerpts.get(url, ""),
        }
        haystack = " ".join(
            [
                title,
                str(observation["snippet"]),
                str(observation["excerpt"]),
            ]
        )
        if (
            _url_is_trusted_content(url)
            and _web_item_is_meaningful(observation)
            and _matches_topic(haystack, _topic_terms(topic), topic)
        ):
            observations.append(observation)
        if len(observations) >= 5:
            break
    if not observations:
        return {
            "kind": "project_followup",
            "title": f"接着上次的「{topic}」",
            "summary": f"接着上次的「{topic}」又找了找，暂时没看到新的东西。",
            "observations": [],
            "tool_calls": [
                {
                    "tool": "project_followup",
                    "query": query,
                    "ok": True,
                    "count": len(results),
                    "fresh": 0,
                }
            ],
            "share_worthy": False,
            "project_topic": topic,
            "project_source": project.source,
            "read_excerpt": "",
            "content_depth": "none",
            "project_rest": True,
        }
    top = observations[0]
    read_excerpt = str(top.get("excerpt") or top.get("snippet") or "").strip()
    depth = "full_text" if len(read_excerpt) >= 200 else "snippet"
    titles = "；".join(item["title"] for item in observations[:3])
    return {
        "kind": "project_followup",
        "title": f"接着上次的「{topic}」",
        "summary": f"接着上次的「{topic}」，又看到：{titles}"[:420],
        "observations": observations,
        "tool_calls": [
            {
                "tool": "project_followup",
                "query": query,
                "ok": True,
                "count": len(results),
                "fresh": len(observations),
            }
        ],
        "share_worthy": True,
        "project_topic": topic,
        "project_source": "web",
        "read_excerpt": read_excerpt[:ARTICLE_EXCERPT_LIMIT],
        "content_depth": depth,
    }


def _next_activity(
    session: Session,
    friend_id: str,
    last_kind: str | None,
) -> tuple[str, LifeProject | None]:
    project = active_project(session, friend_id)
    if project is not None and int(project.step_count or 0) < LIFE_PROJECT_MAX_STEPS:
        return "project_followup", project
    if last_kind == "bilibili_ranking":
        return "web_read", None
    return "bilibili_ranking", None


def _start_activity(
    session: Session,
    friend: Friend,
    life: FriendLife,
    last_kind: str | None,
) -> tuple[LifeActivity, LifeProject | None]:
    from backend.services import sync_service  # noqa: PLC0415

    now = _now()
    kind, project = _next_activity(session, friend.id, last_kind)
    activity = LifeActivity(
        id=str(uuid.uuid4()),
        friend_id=friend.id,
        project_id=project.id if project is not None else None,
        device_id=sync_service.device_id(session),
        kind=kind,
        title="",
        status="running",
        started_at=_iso(now),
        ends_at=None,
        finished_at=None,
        summary="",
        observations="[]",
        tool_calls="[]",
        created_at=_iso(now),
        updated_at=_iso(now),
    )
    session.add(activity)
    session.flush()
    life.current_activity_id = activity.id
    life.paused_by_chat = 0
    life.updated_at = _iso(now)
    return activity, project


def _apply_activity_result(
    session: Session,
    life: FriendLife,
    activity: LifeActivity,
    result: dict[str, Any],
    *,
    project: LifeProject | None = None,
) -> None:
    now = _now()
    duration = random.randint(ACTIVITY_MIN_SECONDS, ACTIVITY_MAX_SECONDS)
    activity.status = "running"
    activity.title = str(result.get("title") or activity.kind)
    activity.summary = str(result.get("summary") or "")
    activity.observations = json.dumps(result.get("observations") or [], ensure_ascii=False)
    activity.tool_calls = json.dumps(result.get("tool_calls") or [], ensure_ascii=False)
    activity.ends_at = _iso(now + timedelta(seconds=duration))
    activity.updated_at = _iso(now)
    life.last_observation = activity.summary
    # D353：只有真的看到东西才进入“待分享”；低质量活动只留在 last_observation。
    if bool(result.get("share_worthy", True)):
        life.pending_share = activity.summary
    # v7.17（D360）：把这次真实阅读接进一条最多三步的生活线。
    if project is not None:
        activity.project_id = project.id
        _continue_project(session, project, result)
    elif bool(result.get("share_worthy", True)):
        created = _create_project(session, activity, result)
        if created is not None:
            activity.project_id = created.id
    life.updated_at = _iso(now)


def _fail_activity(
    session: Session,
    life: FriendLife,
    activity: LifeActivity,
    error: str | None,
) -> None:
    now = _now()
    activity.status = "failed"
    activity.summary = f"这次没做成：{error or '未知原因'}"
    activity.observations = "[]"
    activity.tool_calls = json.dumps(
        [{"tool": activity.kind, "ok": False, "error": (error or "")[:500]}],
        ensure_ascii=False,
    )
    activity.finished_at = _iso(now)
    activity.updated_at = _iso(now)
    life.current_activity_id = None
    life.last_activity_at = _iso(now)
    life.last_observation = activity.summary
    life.updated_at = _iso(now)
    feature_events.emit(
        "life.activity.finished",
        friend_id=activity.friend_id,
        activity_id=activity.id,
        kind=activity.kind,
        ok=False,
    )


def _complete_due_activity(
    session: Session,
    life: FriendLife,
    activity: LifeActivity,
) -> None:
    now = _now()
    activity.status = "completed"
    activity.finished_at = _iso(now)
    activity.updated_at = _iso(now)
    life.current_activity_id = None
    life.last_activity_at = _iso(now)
    if activity.summary:
        life.last_observation = activity.summary
    life.updated_at = _iso(now)
    feature_events.emit(
        "life.activity.finished",
        friend_id=activity.friend_id,
        activity_id=activity.id,
        kind=activity.kind,
        ok=True,
    )


async def tick() -> dict[str, Any]:
    """推进所有已授权朋友的生活线；只由电脑端调度器调用。"""
    import asyncio

    started = 0
    finished = 0
    with session_scope() as session:
        if not _device_is_life_host(session):
            return {"started": 0, "finished": 0, "reason": "not_desktop_life_host"}
        now = _now()
        friends = list(
            session.execute(
                select(Friend).where(
                    Friend.deleted_at.is_(None),
                    Friend.life_enabled == 1,
                )
            ).scalars()
        )
        for friend in friends:
            life = get_or_create_life(session, friend.id)
            paused_until = _parse(life.paused_until)
            friend_pause = _parse(friend.chat_pause_until)
            if (
                (paused_until is not None and paused_until > now)
                or (friend_pause is not None and friend_pause > now)
            ):
                continue
            if life.current_activity_id:
                activity = session.get(LifeActivity, life.current_activity_id)
                if activity is None:
                    life.current_activity_id = None
                    life.updated_at = _iso(now)
                    continue
                ends_at = _parse(activity.ends_at)
                if ends_at is None:
                    started_at = _parse(activity.started_at)
                    if started_at is not None and (now - started_at).total_seconds() >= 120:
                        _fail_activity(
                            session,
                            life,
                            activity,
                            "上次活动在拿到结果前中断了",
                        )
                        finished += 1
                    continue
                if ends_at is not None and ends_at <= now:
                    _complete_due_activity(session, life, activity)
                    finished += 1
                continue
            last_at = _parse(life.last_activity_at)
            if last_at is not None and (now - last_at).total_seconds() < LIFE_COOLDOWN_SECONDS:
                continue
            # 找上一条活动来决定轮换
            previous = session.execute(
                select(LifeActivity)
                .where(LifeActivity.friend_id == friend.id)
                .order_by(LifeActivity.started_at.desc())
                .limit(1)
            ).scalar_one_or_none()
            local_hour = datetime.now().astimezone().hour
            if local_hour < LIFE_ACTIVE_START_HOUR or local_hour >= LIFE_ACTIVE_END_HOUR:
                continue
            activity, project = _start_activity(
                session, friend, life, previous.kind if previous is not None else None
            )
            started += 1
            session.commit()
            # 真实网络动作放到线程，别堵事件循环；执行期间活动保持 running。
            try:
                interests = _interest_keywords(session, friend)
                if activity.kind == "bilibili_ranking":
                    seen = _recent_urls(session, friend.id)
                    result = await asyncio.to_thread(
                        _run_bilibili_ranking,
                        interests,
                        seen,
                    )
                elif activity.kind == "project_followup" and project is not None:
                    seen = _recent_urls(session, friend.id)
                    result = await asyncio.to_thread(
                        _run_project_followup,
                        project,
                        interests,
                        seen,
                    )
                else:
                    result = await asyncio.to_thread(_run_web_read, interests)
            except Exception as exc:  # noqa: BLE001 —— 失败也必须是“她真的试过”
                logger.warning("生活线活动失败（{}）：{}", activity.kind, exc)
                with session_scope() as write_session:
                    fresh_life = write_session.get(FriendLife, friend.id)
                    fresh_activity = write_session.get(LifeActivity, activity.id)
                    if fresh_life is not None and fresh_activity is not None:
                        _fail_activity(write_session, fresh_life, fresh_activity, str(exc))
                continue
            with session_scope() as write_session:
                fresh_life = write_session.get(FriendLife, friend.id)
                fresh_activity = write_session.get(LifeActivity, activity.id)
                if fresh_life is None or fresh_activity is None:
                    continue
                fresh_project = (
                    write_session.get(LifeProject, project.id)
                    if project is not None
                    else None
                )
                _apply_activity_result(
                    write_session,
                    fresh_life,
                    fresh_activity,
                    result,
                    project=fresh_project,
                )
    return {"started": started, "finished": finished}


def status_for(session: Session, friend: Friend) -> dict[str, Any]:
    life = get_or_create_life(session, friend.id)
    now = _now()
    friend_pause = _parse(friend.chat_pause_until)
    life_pause = _parse(life.paused_until)
    paused = bool(
        life.paused_by_chat
        or (friend_pause is not None and friend_pause > now)
        or (life_pause is not None and life_pause > now)
    )
    current = None
    if life.current_activity_id:
        activity = session.get(LifeActivity, life.current_activity_id)
        if activity is not None:
            current = activity_to_dict(activity)
    recent_rows = list(
        session.execute(
            select(LifeActivity)
            .where(LifeActivity.friend_id == friend.id)
            .order_by(LifeActivity.started_at.desc())
            .limit(8)
        ).scalars()
    )
    return {
        "friend_id": friend.id,
        "enabled": bool(friend.life_enabled),
        "paused_by_chat": paused,
        "paused_until": life.paused_until or friend.chat_pause_until,
        "last_observation": life.last_observation,
        "pending_share": life.pending_share,
        "last_share_at": life.last_share_at,
        "project": (
            project_to_dict(project)
            if (project := latest_project(session, friend.id)) is not None
            else None
        ),
        "current_activity": current,
        "recent": [activity_to_dict(item) for item in recent_rows],
    }


def activity_to_dict(activity: LifeActivity) -> dict[str, Any]:
    return {
        "id": activity.id,
        "friend_id": activity.friend_id,
        "project_id": activity.project_id,
        "device_id": activity.device_id,
        "kind": activity.kind,
        "title": activity.title,
        "status": activity.status,
        "started_at": activity.started_at,
        "ends_at": activity.ends_at,
        "finished_at": activity.finished_at,
        "summary": activity.summary,
        "observations": _loads(activity.observations, []),
        "tool_calls": _loads(activity.tool_calls, []),
        "updated_at": activity.updated_at,
    }


def note_for_prompt(session: Session, friend_id: str) -> str:
    """给聊天 / 主动消息注入“她刚才在做什么、这条生活线走到哪了”的事实。"""
    friend = session.get(Friend, friend_id)
    if friend is None or not friend.life_enabled:
        return ""
    status = status_for(session, friend)
    lines: list[str] = []
    current = status.get("current_activity")
    if isinstance(current, dict) and current.get("title"):
        lines.append(f"你现在在做：{current['title']}")
    last = str(status.get("last_observation") or "").strip()
    if last:
        lines.append(f"你上次活动的真实观察：{last}")
    project_note = project_prompt_note(session, friend_id)
    if project_note:
        lines.append(project_note)
    recent = status.get("recent") or []
    summaries = [
        str(item.get("summary") or "").strip()
        for item in recent
        if isinstance(item, dict) and str(item.get("summary") or "").strip()
    ][:3]
    if summaries:
        lines.append("你最近做过：" + "；".join(summaries))
    if not lines:
        return ""
    return (
        "# 你自己的生活在进行\n"
        + "\n".join(lines)
        + "\n（这些是你自己的事实，不是用户交代的任务。聊天时自然带出来；不用报流水账。）"
    )
