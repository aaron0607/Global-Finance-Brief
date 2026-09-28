#!/usr/bin/env python3
"""财经简报本地新闻聚合脚本

聚合 Bloomberg/CNBC RSS、Reuters 官方 news sitemap（失败时 Bing News 兜底）、
Futu 牛牛新闻搜索、金十数据（jin10 远程 MCP）、华尔街见闻美股直播、alphaear (NewsNow API)，
输出统一 news_feed.json 供内容编辑使用。入池优先级由 write_brief.build_news_pool 按来源分级决定。

金十 token 从环境变量 JIN10_MCP_TOKEN 读取（未配置则跳过并告警）。

用法:
    .venv/bin/python fetch_news.py

输出: 本目录 news_feed.json
"""

from __future__ import annotations

import datetime as dt
import html
import json
import os
import re
import sys
from pathlib import Path
from typing import Optional
from zoneinfo import ZoneInfo

import requests

BRIEF_TZ = ZoneInfo("Asia/Shanghai")
BASE_DIR = Path(__file__).parent

# ── AlphaEar / NewsNow 财经源 ──────────────────────────────
ALPHAEAR_BASE = "https://newsnow.busiyi.world"
ALPHAEAR_SOURCES = {
    "cls":          {"name": "财联社",     "category": "finance"},
    "wallstreetcn": {"name": "华尔街见闻",  "category": "finance"},
    "xueqiu":       {"name": "雪球热榜",    "category": "finance"},
}

# ── RSS 源（Bloomberg / CNBC）────────────────────────────
RSS_FEEDS = {
    "bloomberg": {
        "name": "Bloomberg Markets",
        "url": "https://www.bloomberg.com/feeds/markets/news.rss",
        "category": "finance",
    },
    "cnbc": {
        "name": "CNBC Top News",
        "url": "https://search.cnbc.com/rs/search/combinedcms/view.xml?partnerId=wrss01&id=100003114",
        "category": "finance",
    },
}

# ── Reuters ────────────────────────────────────────────
# Reuters 无公开 RSS、文章页反爬，但官方 Google News sitemap 可直连：每页 50 条、按发布时间倒序，
# 带精确 news:publication_date。按栏目白名单只保留英文财经/科技/国际新闻。
# sitemap 不可达（如服务器网络限制）时退回 Bing News RSS（site:reuters.com），其 pubDate 精度较差，
# 只保留 URL 日期与 pubDate 相差不超过 1 天的条目。
REUTERS_SITEMAP_URL = "https://www.reuters.com/arc/outboundfeeds/news-sitemap/?outputType=xml&from={offset}"
# 栏目白名单（URL 路径前缀）：财经/科技全收；国际新闻只收影响定价的美国、中国、中东（地缘/原油）。
REUTERS_SECTIONS = (
    "business/", "markets/", "technology/", "commentary/breakingviews/",
    "world/us/", "world/china/", "world/middle-east/",
)
REUTERS_SECTIONS_EXCLUDE = ("business/environment/",)  # 多为天气/灾害，与定价弱相关
REUTERS_LOOKBACK_HOURS = 26
REUTERS_MAX_ITEMS = 40
REUTERS_MAX_PAGES = 20
REUTERS_TIME_BUDGET_S = 60
BING_NEWS_URL = "https://www.bing.com/news/search"
BING_REUTERS_QUERIES = ("markets", "stocks", "technology", "economy Fed", "oil gold")
REUTERS_URL_DATE = re.compile(r"-(\d{4}-\d{2}-\d{2})/?$")

# ── Futu 牛牛新闻搜索（公共 HTTP API，无需 OpenD/鉴权）────────
# 供写稿阶段按关键词补强中文财经/个股新闻；keyword 可在调用时覆盖。
FUTU_BASE = "https://ai-news-search.futunn.com/news_search"
FUTU_UA = "futunn-news-search/0.0.2 (Skill)"
FUTU_KEYWORDS = ["英伟达", "美联储", "黄金", "原油", "半导体", "科技股", "AI"]

# ── 金十数据（通过 jin10 远程 MCP 的 HTTP 协议直连）────────
# token 从环境变量 JIN10_MCP_TOKEN 读取（服务器部署时写入 .secrets.env，chmod 600），
# 不写入仓库。未配置时跳过金十并告警。
JIN10_MCP_URL = "https://mcp.jin10.com/mcp"
JIN10_MCP_TOKEN = os.environ.get("JIN10_MCP_TOKEN", "").strip()

# ── 华尔街见闻 · 美股直播（实时美股新闻）────────────────
WSCN_LIVE_API = "https://api-one.wallstcn.com/apiv1/content/lives"
WSCN_LIVE_CHANNEL = "us-stock-channel"

USER_AGENT = "Mozilla/5.0 (compatible; BriefBot/1.0)"
SESSION = requests.Session()
SESSION.headers.update({"User-Agent": USER_AGENT})


def _fetch_json(url: str, timeout: int = 15) -> Optional[dict]:
    """安全抓取 JSON 端点。"""
    try:
        resp = SESSION.get(url, timeout=timeout)
        if resp.status_code != 200:
            print(f"[warn] {url} → HTTP {resp.status_code}", file=sys.stderr)
            return None
        return resp.json()
    except Exception as exc:
        print(f"[warn] 无法获取 {url}: {exc}", file=sys.stderr)
        return None


def _fetch_text(url: str, timeout: int = 15) -> Optional[str]:
    """安全抓取文本（RSS XML）。"""
    try:
        resp = SESSION.get(url, timeout=timeout)
        if resp.status_code != 200:
            print(f"[warn] {url} → HTTP {resp.status_code}", file=sys.stderr)
            return None
        return resp.text
    except Exception as exc:
        print(f"[warn] 无法获取 {url}: {exc}", file=sys.stderr)
        return None


def _parse_rss(xml_text: str) -> list[dict]:
    """极简 RSS 解析——提取 <item> 的 title/link/pubDate/description。"""
    items = []
    for match in re.finditer(r"<item>(.*?)</item>", xml_text, re.DOTALL):
        block = match[1]
        title = _tag_text(block, "title")
        link = _tag_text(block, "link")
        pub_date = _tag_text(block, "pubDate")
        desc = _tag_text(block, "description")
        if title:
            items.append({
                "title": _clean_title(title),
                "url": link,
                "published_at": _try_parse_rss_date(pub_date),
                "summary": _strip_html(desc)[:300] if desc else "",
            })
    return items


def _clean_title(raw: str) -> str:
    """去除 CDATA 包装和 HTML 实体。"""
    return _strip_html(raw)


def _tag_text(block: str, tag: str) -> str:
    m = re.search(rf"<{tag}[^>]*>(.*?)</{tag}>", block, re.DOTALL)
    return m.group(1).strip() if m else ""


def _strip_html(raw: str) -> str:
    # 先拆 CDATA：否则 `<![CDATA[ ... ]]>` 整体会被当成一个标签替换成空格，Bloomberg/CNBC 摘要全丢。
    text = re.sub(r"<!\[CDATA\[(.*?)\]\]>", r"\1", raw, flags=re.DOTALL)
    text = html.unescape(re.sub(r"<[^>]+>", " ", text))
    return re.sub(r"\s+", " ", text).strip()


def _try_parse_rss_date(raw: str) -> Optional[str]:
    """解析 RSS pubDate → 带时区的 ISO 8601。"""
    if not raw:
        return None
    clean = _clean_title(raw).strip()
    for fmt in (
        "%a, %d %b %Y %H:%M:%S %Z",
        "%a, %d %b %Y %H:%M:%S %z",
        "%Y-%m-%dT%H:%M:%S%z",
    ):
        try:
            parsed = dt.datetime.strptime(clean, fmt)
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=dt.timezone.utc)
            return parsed.astimezone(BRIEF_TZ).isoformat()
        except ValueError:
            continue
    return clean


def fetch_alphaear(source_id: str, count: int = 15) -> list[dict]:
    """从 alphaear NewsNow API 获取指定源的热榜。"""
    url = f"{ALPHAEAR_BASE}/api/s?id={source_id}"
    data = _fetch_json(url)
    if not data:
        return []
    items = data.get("items", [])
    results = []
    ts = data.get("updatedTime")
    fetched_at = (
        dt.datetime.fromtimestamp(ts / 1000, tz=BRIEF_TZ).isoformat()
        if ts else None
    )
    for it in items[:count]:
        results.append({
            "source": "alphaear",
            "source_name": ALPHAEAR_SOURCES.get(source_id, {}).get("name", source_id),
            "source_id": source_id,
            "title": it.get("title", ""),
            "url": it.get("url", it.get("mobileUrl", "")),
            "summary": it.get("summary", it.get("description", ""))[:300],
            "published_at": fetched_at,
        })
    return results


def fetch_rss(feed_id: str) -> list[dict]:
    """从一条 RSS feed 获取最新条目。"""
    feed = RSS_FEEDS.get(feed_id)
    if not feed:
        return []
    xml_text = _fetch_text(feed["url"])
    if not xml_text:
        return []
    items = _parse_rss(xml_text)
    return [
        {
            "source": "rss",
            "source_name": feed["name"],
            "source_id": feed_id,
            "title": it["title"],
            "url": it.get("url", ""),
            "summary": it.get("summary", ""),
            "published_at": it.get("published_at"),
        }
        for it in items[:20]
    ]


def _reuters_section_ok(url: str) -> bool:
    m = re.match(r"https://www\.reuters\.com/(.+)", url or "")
    return bool(m) and m.group(1).startswith(REUTERS_SECTIONS) \
        and not m.group(1).startswith(REUTERS_SECTIONS_EXCLUDE)


def _reuters_item(title: str, url: str, published: dt.datetime, summary: str = "") -> dict:
    return {
        "source": "reuters",
        "source_name": "Reuters",
        "source_id": "reuters",
        "title": title.strip(),
        "url": url,
        "summary": summary.strip()[:300],
        "published_at": published.astimezone(BRIEF_TZ).isoformat(),
    }


def parse_reuters_sitemap(xml_text: str) -> list[tuple[dt.datetime, dict]]:
    """解析一页 news sitemap → [(发布时间, item)]，含被栏目白名单过滤掉的条目时间（用于翻页判停）。"""
    out = []
    for block in re.findall(r"<url>(.*?)</url>", xml_text, re.DOTALL):
        loc = _tag_text(block, "loc")
        raw_date = _tag_text(block, "news:publication_date")
        title = _clean_title(_tag_text(block, "news:title"))
        try:
            published = dt.datetime.fromisoformat(raw_date.replace("Z", "+00:00"))
        except ValueError:
            continue
        if published.tzinfo is None:
            continue
        item = _reuters_item(title, loc, published) if title and _reuters_section_ok(loc) else None
        out.append((published, item))
    return out


def fetch_reuters_sitemap(now: Optional[dt.datetime] = None) -> list[dict]:
    now = now or dt.datetime.now(dt.timezone.utc)
    cutoff = now - dt.timedelta(hours=REUTERS_LOOKBACK_HOURS)
    started = dt.datetime.now(dt.timezone.utc)
    items, seen = [], set()
    for page in range(REUTERS_MAX_PAGES):
        if (dt.datetime.now(dt.timezone.utc) - started).total_seconds() > REUTERS_TIME_BUDGET_S:
            print("[warn] reuters sitemap 翻页超时，已停止", file=sys.stderr)
            break
        resp = SESSION.get(REUTERS_SITEMAP_URL.format(offset=page * 50), timeout=15)
        resp.raise_for_status()
        rows = parse_reuters_sitemap(resp.text)
        if not rows:
            break
        for published, item in rows:
            if item and published >= cutoff and item["url"] not in seen:
                seen.add(item["url"])
                items.append(item)
        if min(published for published, _ in rows) < cutoff:
            break
    items.sort(key=lambda it: it["published_at"], reverse=True)
    return items[:REUTERS_MAX_ITEMS]


def parse_bing_reuters(xml_text: str) -> list[dict]:
    """Bing News RSS → Reuters 条目。只收 News:Source=Reuters、真实 reuters.com 文章 URL，
    且 URL 日期与 pubDate 相差不超过 1 天（Bing 的 pubDate 常有数小时偏差）。"""
    from urllib.parse import parse_qs, urlparse

    items = []
    for block in re.findall(r"<item>(.*?)</item>", xml_text, re.DOTALL):
        if _tag_text(block, "News:Source").strip() != "Reuters":
            continue
        link = _tag_text(block, "link").replace("&amp;", "&")
        url = (parse_qs(urlparse(link).query).get("url") or [link])[0]
        if not _reuters_section_ok(url):
            continue
        slug = REUTERS_URL_DATE.search(url)
        try:
            published = dt.datetime.strptime(_tag_text(block, "pubDate"), "%a, %d %b %Y %H:%M:%S %Z").replace(tzinfo=dt.timezone.utc)
            slug_date = dt.date.fromisoformat(slug.group(1)) if slug else None
        except ValueError:
            continue
        if not slug_date or abs((published.date() - slug_date).days) > 1:
            continue
        items.append(_reuters_item(_clean_title(_tag_text(block, "title")), url, published,
                                   _strip_html(_tag_text(block, "description"))))
    return items


def fetch_reuters_bing() -> list[dict]:
    items, seen = [], set()
    for query in BING_REUTERS_QUERIES:
        try:
            resp = SESSION.get(BING_NEWS_URL, params={
                "q": "site:reuters.com " + query, "format": "rss", "cc": "us",
                "qft": 'sortbydate="1"',
            }, timeout=15)
            resp.raise_for_status()
        except Exception as exc:  # noqa: BLE001 - 单查询失败不阻断
            print(f"[warn] reuters-bing/{query}: {exc}", file=sys.stderr)
            continue
        for item in parse_bing_reuters(resp.text):
            if item["url"] not in seen:
                seen.add(item["url"])
                items.append(item)
    items.sort(key=lambda it: it["published_at"], reverse=True)
    return items[:REUTERS_MAX_ITEMS]


def fetch_reuters() -> list[dict]:
    """Reuters：官方 news sitemap 优先，失败或 0 条时退回 Bing News。"""
    try:
        items = fetch_reuters_sitemap()
        if items:
            return items
        print("[warn] reuters sitemap 0 条，改用 Bing News 兜底", file=sys.stderr)
    except Exception as exc:  # noqa: BLE001
        print(f"[warn] reuters sitemap 失败（{exc}），改用 Bing News 兜底", file=sys.stderr)
    return fetch_reuters_bing()


def _format_epoch(value) -> Optional[str]:
    """futu publish_time 为 unix 秒（或毫秒）→ 带时区的 ISO 8601。"""
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    if number > 1e12:
        number = number / 1000
    return dt.datetime.fromtimestamp(number, tz=BRIEF_TZ).isoformat()


def fetch_futu(keywords: Optional[list[str]] = None, size: int = 10) -> list[dict]:
    """从富途牛牛公共新闻搜索接口按关键词聚合，无需 OpenD/鉴权。"""
    for kw in keywords or FUTU_KEYWORDS:
        try:
            resp = SESSION.get(
                FUTU_BASE,
                params={"keyword": kw, "size": size, "news_type": 1, "lang": "zh-CN", "sort_type": 2},
                headers={"User-Agent": FUTU_UA},
                timeout=15,
            )
            if resp.status_code != 200:
                print(f"[warn] futu/{kw} → HTTP {resp.status_code}", file=sys.stderr)
                continue
            payload = resp.json()
            if payload.get("code") != 0:
                print(f"[warn] futu/{kw} → code={payload.get('code')}", file=sys.stderr)
                continue
            for item in payload.get("data", [])[:size]:
                yield {
                    "source": "futu",
                    "source_name": "富途牛牛",
                    "source_id": "futu-news-search",
                    "title": re.sub(r"</?em>", "", item.get("title", "")),
                    "url": item.get("url", ""),
                    "summary": "",
                    "published_at": _format_epoch(item.get("publish_time")),
                }
        except Exception as exc:  # noqa: BLE001 - 单关键词失败不阻断
            print(f"[warn] futu/{kw}: {exc}", file=sys.stderr)


def jin10_call(tool: str, arguments: dict, timeout: int = 20) -> Optional[dict]:
    """最小 MCP HTTP 客户端：initialize → notifications/initialized → tools/call。
    返回工具返回的 JSON dict；失败返回 None。"""
    if not JIN10_MCP_TOKEN:
        return None
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
        "Authorization": "Bearer " + JIN10_MCP_TOKEN,
    }
    try:
        init = SESSION.post(
            JIN10_MCP_URL, headers=headers, timeout=timeout,
            json={"jsonrpc": "2.0", "id": 1, "method": "initialize",
                  "params": {"protocolVersion": "2024-11-05", "capabilities": {},
                             "clientInfo": {"name": "brief", "version": "1.0"}}},
        )
        if init.status_code != 200:
            print("[warn] 金十 MCP 初始化失败 HTTP %d" % init.status_code, file=sys.stderr)
            return None
        # 金十服务端是无会话实现：不返回 Mcp-Session-Id，也不需要它。
        # 旧版把缺失该头当成初始化失败，导致金十源在服务器上长期静默为 0 条。
        session_id = init.headers.get("Mcp-Session-Id")
        if session_id:
            headers["Mcp-Session-Id"] = session_id
        SESSION.post(JIN10_MCP_URL, headers=headers, timeout=timeout,
                     json={"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}})
        resp = SESSION.post(
            JIN10_MCP_URL, headers=headers, timeout=timeout,
            json={"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                  "params": {"name": tool, "arguments": arguments}},
        )
        payload = None
        # 注意：text/event-stream 无 charset，requests 的 .text 会按 latin-1 解码，
        # 必须显式按 UTF-8 解码响应字节，否则 JSON 解析与中文都错乱。
        for line in resp.content.decode("utf-8", "replace").splitlines():
            if line.startswith("data: "):
                try:
                    obj = json.loads(line[6:])
                except (ValueError, TypeError):
                    continue
                if obj.get("id") == 2 or "result" in obj:
                    payload = obj
        if not payload or "result" not in payload:
            print("[warn] 金十 MCP %s 无结果" % tool, file=sys.stderr)
            return None
        content = payload["result"].get("content") or []
        text = "".join(part.get("text", "") for part in content if part.get("type") == "text")
        return json.loads(text) if text else None
    except Exception as exc:  # noqa: BLE001 - 单源失败不阻断
        print("[warn] 金十 MCP %s: %s" % (tool, exc), file=sys.stderr)
        return None


def fetch_jin10(flash_limit: int = 30, news_limit: int = 15) -> list[dict]:
    """从金十远程 MCP 拉取最新快讯与文章（带时区时间，可直接进入 24h 窗口校验）。"""
    items = []
    if not JIN10_MCP_TOKEN:
        print("[warn] 未配置 JIN10_MCP_TOKEN，跳过金十", file=sys.stderr)
        return items
    flash = jin10_call("list_flash", {})
    if flash:
        for item in (flash.get("data", {}).get("items") or [])[:flash_limit]:
            content = item.get("content", "")
            items.append({
                "source": "jin10",
                "source_name": "金十数据快讯",
                "source_id": "jin10-flash",
                "title": _clean_title(content)[:120],
                "url": item.get("url", ""),
                "summary": _strip_html(content)[:300],
                "published_at": item.get("time"),
            })
    news = jin10_call("list_news", {})
    if news:
        for item in (news.get("data", {}).get("items") or [])[:news_limit]:
            items.append({
                "source": "jin10",
                "source_name": "金十数据",
                "source_id": "jin10-news",
                "title": item.get("title", ""),
                "url": item.get("url", ""),
                "summary": (item.get("summary") or item.get("content") or "")[:300],
                "published_at": item.get("time") or item.get("published_at"),
            })
    return items


_jin10_call = jin10_call  # 兼容旧调用名


def fetch_wscn_live(limit: int = 20) -> list[dict]:
    """华尔街见闻美股直播实时新闻（api-one.wallstcn.com 干净 JSON）。"""
    items = []
    try:
        resp = SESSION.get(WSCN_LIVE_API, params={"channel": WSCN_LIVE_CHANNEL, "limit": limit}, timeout=15)
        if resp.status_code != 200:
            print(f"[warn] wallstreetcn live → HTTP {resp.status_code}", file=sys.stderr)
            return items
        data = resp.json()
        if data.get("code") != 20000:
            print(f"[warn] wallstreetcn live code={data.get('code')}", file=sys.stderr)
            return items
        for item in (data.get("data", {}).get("items") or [])[:limit]:
            text = re.sub(r"<[^>]+>", " ", item.get("content_text") or "").strip()
            if not text:
                continue
            title = _strip_html(item.get("highlight_title") or "")[:120] or text[:120]
            items.append({
                "source": "wallstreetcn",
                "source_name": "华尔街见闻·美股直播",
                "source_id": "wscn-live",
                "title": title,
                "url": item.get("uri", ""),
                "summary": text[:300],
                "published_at": _format_epoch(item.get("display_time")),
            })
    except Exception as exc:  # noqa: BLE001 - 单源失败不阻断
        print(f"[warn] wallstreetcn live: {exc}", file=sys.stderr)
    return items


def fetch_all() -> list[dict]:
    """聚合全部财经新闻来源。

    顺序仅为可读性（海外 → 国内快讯 → 国内热榜），入池优先级由 write_brief 按来源分级决定。
    """
    all_items = []

    # 海外一手媒体：RSS（Bloomberg / CNBC）+ Reuters
    for feed_id in RSS_FEEDS:
        items = fetch_rss(feed_id)
        all_items.extend(items)
        print(f"  rss/{feed_id}: {len(items)} 条")
    reuters_items = fetch_reuters()
    all_items.extend(reuters_items)
    print(f"  reuters: {len(reuters_items)} 条")

    # Futu 牛牛新闻搜索
    futu_items = list(fetch_futu())
    all_items.extend(futu_items)
    print(f"  futu-news-search: {len(futu_items)} 条")

    # 金十（jin10 远程 MCP）
    jin10_items = fetch_jin10()
    all_items.extend(jin10_items)
    print(f"  jin10: {len(jin10_items)} 条")

    # 华尔街见闻·美股直播
    wscn_items = fetch_wscn_live()
    all_items.extend(wscn_items)
    print(f"  wscn-live: {len(wscn_items)} 条")

    # alphaear 国内热榜
    for src_id in ALPHAEAR_SOURCES:
        items = fetch_alphaear(src_id)
        all_items.extend(items)
        print(f"  alphaear/{src_id}: {len(items)} 条")

    return all_items


def main():
    print("→ 聚合新闻来源 (Bloomberg/CNBC RSS + Reuters + Futu + 金十 + 华尔街见闻 + alphaear) ...", file=sys.stderr)
    items = fetch_all()
    out_path = BASE_DIR / "news_feed.json"
    payload = {
        "fetched_at": dt.datetime.now(BRIEF_TZ).isoformat(),
        "item_count": len(items),
        "items": items,
    }
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    print(f"已写入: {out_path} ({len(items)} 条)", file=sys.stderr)


if __name__ == "__main__":
    main()
