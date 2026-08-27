#!/usr/bin/env python3
"""财经简报本地新闻聚合脚本

聚合 alphaear (NewsNow API)、Bloomberg/CNBC RSS、Futu 牛牛新闻搜索、金十数据（jin10 远程 MCP），
输出统一 news_feed.json 供内容编辑使用。

金十 token 从环境变量 JIN10_MCP_TOKEN 读取（未配置则跳过并告警）。

用法:
    .venv/bin/python fetch_news.py

输出: 本目录 news_feed.json
"""

from __future__ import annotations

import datetime as dt
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
# Reuters 无公开 RSS（reutersagency.com/feed 全部 404、reuters.com 反爬 401），
# 由调用方通过 WebSearch(domain:reuters.com) 获取，不在脚本内抓取。
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
    t = re.sub(r"<!\[CDATA\[(.*?)\]\]>", r"\1", raw)
    return _strip_html(t)


def _tag_text(block: str, tag: str) -> str:
    m = re.search(rf"<{tag}[^>]*>(.*?)</{tag}>", block, re.DOTALL)
    return m.group(1).strip() if m else ""


def _strip_html(raw: str) -> str:
    return re.sub(r"<[^>]+>", " ", raw).replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">").replace("&quot;", '"')


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


def _jin10_call(tool: str, arguments: dict, timeout: int = 20) -> Optional[dict]:
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
        session_id = init.headers.get("Mcp-Session-Id")
        if not session_id:
            print("[warn] 金十 MCP 初始化失败", file=sys.stderr)
            return None
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
    flash = _jin10_call("list_flash", {})
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
    news = _jin10_call("list_news", {})
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

    Reuters 因无公开 RSS/反爬，由调用方用 WebSearch(domain:reuters.com) 补充。
    """
    all_items = []

    # alphaear 财经源
    for src_id in ALPHAEAR_SOURCES:
        items = fetch_alphaear(src_id)
        all_items.extend(items)
        print(f"  alphaear/{src_id}: {len(items)} 条")

    # RSS 财经源
    for feed_id in RSS_FEEDS:
        items = fetch_rss(feed_id)
        all_items.extend(items)
        print(f"  rss/{feed_id}: {len(items)} 条")

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

    return all_items


def main():
    print("→ 聚合新闻来源 (alphaear + Bloomberg/CNBC RSS + Futu) ...", file=sys.stderr)
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
