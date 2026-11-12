"""联网搜索（v2.0 / T2.0-7）：**Bing 搜索 + 抓网页正文**，自己给模型喂料。

**为什么不走"模型原生联网"**（2026-09-19 实测）：

```
tools=[{"type":"web_search"}]         → 400 Invalid tool specification
tools=[{"type":"web_search_preview"}] → 400 Invalid tool specification
qwen/…-uncensored:online              → 网关不认这个后缀（超时 / Model not supported）
模型元数据里 web_search / online / grounding / brows* 出现 0 次
```

所以只能自己搜。**搜索源为什么是 cn.bing.com**（同一天实测，直连 / 走梯子两列）：

```
cn.bing.com      ✅ 1.3s / ✅ 2.7s     ← 选它：不要 key、直连就通
DuckDuckGo       ✗ 超时 / ✅ 3.5s
Searx 公共实例    ✗ / ✗
Baidu            ✅ 但反爬（只回 1.4KB，拿不到结果）
```

三条纪律（milestones v2.0 验收）：

1. **带来源**：结果带编号 `[1]`，由上层落进 `messages.sources`，前端渲染成小链接。
2. **查不到就如实说**：连不上 / 没结果 → 抛人话错误（工具层回灌给模型），
   让模型说"我查不到"，**不许编**。
3. **同一问题短时间内不重复请求**：进程内缓存（默认 10 分钟）。

⚠️ 已知代价：工具层是**同步**的，这一整段（搜索 + 抓正文）会占住事件循环几秒
（典型 2~6 秒，最坏 ~15 秒）。工具异步化记在 TASKS 里（v2.1 的文件类工具更需要）。
"""

from __future__ import annotations

import html
import re
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

import httpx
from loguru import logger

from backend.llm.openai_compat import proxy_chain, resolve_proxy
from backend.services.errors import ServiceError

SEARCH_URL = "https://cn.bing.com/search"
CACHE_TTL_SECONDS = 600.0
SEARCH_TIMEOUT = 8.0
PAGE_TIMEOUT = 6.0
MAX_RESULTS = 5
MAX_PAGES = 2
MAX_EXCERPT_CHARS = 1500

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
}

_cache: dict[str, tuple[float, dict]] = {}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _client_kwargs(proxy_value: str) -> dict:
    proxy_url, trust_env = resolve_proxy(proxy_value)
    return {"proxy": proxy_url, "trust_env": trust_env}


def _get(
    url: str,
    *,
    proxy: str,
    params: dict | None = None,
    timeout: float,
) -> httpx.Response:
    """按 D52 那套走法依次试（直连 / 系统代理 / 显式代理）；全失败就抛。"""
    last: Exception | None = None
    for value in proxy_chain(proxy):
        try:
            with httpx.Client(
                timeout=httpx.Timeout(timeout, connect=5.0),
                follow_redirects=True,
                headers=_HEADERS,
                **_client_kwargs(value),
            ) as client:
                return client.get(url, params=params)
        except httpx.HTTPError as exc:
            last = exc
    raise ServiceError(
        "SEARCH_UNREACHABLE",
        "连不上搜索（可能是没网，或者梯子断了）—— 查不到就如实告诉 TA",
        503,
    ) from last


def parse_bing(body: str, *, limit: int = MAX_RESULTS) -> list[dict]:
    """从 Bing 结果页里抠出 title / url / snippet（只认 `<li class="b_algo">` 那种块）。"""
    results: list[dict] = []
    for block in re.findall(r'<li class="b_algo".*?</li>', body, re.S):
        match = re.search(r'<h2[^>]*>\s*<a[^>]*href="([^"]+)"[^>]*>(.*?)</a>', block, re.S)
        if not match:
            continue
        url = html.unescape(match.group(1)).strip()
        title = html.unescape(re.sub(r"<[^>]+>", "", match.group(2))).strip()
        snippet = ""
        for pattern in (r'<p[^>]*>(.*?)</p>', r'<div class="b_caption"[^>]*>(.*?)</div>'):
            found = re.search(pattern, block, re.S)
            if found:
                snippet = html.unescape(re.sub(r"<[^>]+>", " ", found.group(1)))
                snippet = re.sub(r"\s+", " ", snippet).strip()
                break
        if url and title:
            results.append({"title": title, "url": url, "snippet": snippet[:300]})
        if len(results) >= limit:
            break
    return results


def strip_html(body: str) -> str:
    """正文抽取：去脚本/样式/标签，压空白。够用就行（不引第三方解析库）。"""
    body = re.sub(r"(?is)<(script|style|noscript|svg|head)[^>]*>.*?</\1>", " ", body)
    body = re.sub(r"(?s)<[^>]+>", " ", body)
    return re.sub(r"\s+", " ", html.unescape(body)).strip()


def _excerpt(url: str, *, proxy: str) -> dict | None:
    try:
        response = _get(url, proxy=proxy, timeout=PAGE_TIMEOUT)
    except ServiceError:
        return None
    if response.status_code >= 400 or "text/html" not in response.headers.get("content-type", ""):
        return None
    text = strip_html(response.text)
    if not text:
        return None
    return {"url": url, "text": text[:MAX_EXCERPT_CHARS]}


def search(query: str, *, proxy: str = "", force: bool = False) -> dict:
    """搜一次（带缓存）。返回 `{query, results:[{no,title,url,snippet}], excerpts, cached, searched_at}`。"""
    cleaned = (query or "").strip()
    if not cleaned:
        raise ValueError("搜索关键词不能是空的")
    key = cleaned.lower()
    now = time.monotonic()
    if not force:
        hit = _cache.get(key)
        if hit and now - hit[0] < CACHE_TTL_SECONDS:
            payload = dict(hit[1])
            payload["cached"] = True
            return payload

    response = _get(SEARCH_URL, proxy=proxy, params={"q": cleaned}, timeout=SEARCH_TIMEOUT)
    if response.status_code >= 400:
        raise ServiceError(
            "SEARCH_FAILED",
            f"搜索服务回了 HTTP {response.status_code} —— 查不到就如实说",
            502,
        )
    found = parse_bing(response.text)
    if not found:
        raise ServiceError(
            "SEARCH_EMPTY",
            "这次没搜到任何结果（换个说法再试，或者如实告诉 TA 查不到）",
            404,
        )

    results = [
        {"no": index + 1, "title": item["title"], "url": item["url"], "snippet": item["snippet"]}
        for index, item in enumerate(found)
    ]

    excerpts: list[dict] = []
    targets = [item["url"] for item in results[:MAX_PAGES]]
    if targets:
        with ThreadPoolExecutor(max_workers=len(targets)) as pool:
            for piece in pool.map(lambda url: _excerpt(url, proxy=proxy), targets):
                if piece:
                    excerpts.append(piece)

    payload = {
        "query": cleaned,
        "results": results,
        "excerpts": excerpts,
        "cached": False,
        "searched_at": _now_iso(),
    }
    _cache[key] = (now, payload)
    logger.info("搜网：{!r} → {} 条结果，正文 {} 篇", cleaned[:40], len(results), len(excerpts))
    return dict(payload)


def clear_cache() -> None:
    """测试用。"""
    _cache.clear()
