#!/usr/bin/env python3
"""财经简报「本周财经日历」确定性采集脚本。

日历此前由写稿模型凭新闻池推断，经常出现空表或日期错位（2026-08-29/08-30 日历为空，
08-31 把 9 月 4 日的非农写成当天）。本脚本改为从两个结构化日历源取数，
写出 calendar_feed.json，供 write_brief.py 直接回填，模型不再决定日期。

数据源（国内可直连，均返回北京时间）：
  1) 金十数据日历（jin10 远程 MCP list_calendar）：本自然周经济数据 + 重要性星级，主源；
     需要 JIN10_MCP_TOKEN（服务器 .secrets.env）。
  2) 东方财富财经日历（RPT_CPH_FECALENDAR）：未来 N 天的会议/发布会等事件与经济数据，
     无需鉴权，既是金十缺失时的兜底，也补齐跨周（金十只给本自然周）的条目。

用法:
    .venv/bin/python fetch_calendar.py [--days 7] [--max 5] [--min 3] [--out calendar_feed.json]

输出: 本目录 calendar_feed.json；无任何有效条目时退出码 1。
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import sys
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

try:  # 复用 fetch_news.py 里已实测的金十 MCP 客户端，避免两份实现漂移
    from fetch_news import JIN10_MCP_TOKEN, jin10_call
except ImportError:  # pragma: no cover - 仅在脚本被单独拷贝时触发
    JIN10_MCP_TOKEN, jin10_call = "", None

BRIEF_TZ = ZoneInfo("Asia/Shanghai")
BASE_DIR = Path(__file__).parent
OUT_PATH = BASE_DIR / "calendar_feed.json"
SCHEMA_VERSION = 1
WEEKDAYS = "一二三四五六日"

WINDOW_DAYS = 7          # 展望窗口：as_of 起 7 个自然日
MAX_EVENTS = 5           # 成稿日历最多几行
MIN_EVENTS = 3           # 少于这个条数就逐级放宽阈值
SCORE_THRESHOLDS = (7, 6, 5)

EASTMONEY_API = "https://datacenter-web.eastmoney.com/api/data/v1/get"
EASTMONEY_COLUMNS = "START_DATE,END_DATE,FE_CODE,FE_NAME,FE_TYPE,CONTENT,STD_TYPE_CODE,SPONSOR_NAME,CITY"

# 地区权重：简报面向美股/黄金读者，美国最高，欧元区与中国次之。
REGIONS = ["中国香港", "中国台湾", "欧元区", "美国", "中国", "德国", "英国", "日本", "法国",
           "加拿大", "澳大利亚", "新西兰", "瑞士", "韩国", "意大利", "西班牙", "欧盟",
           "印度", "巴西", "俄罗斯", "越南", "南非", "墨西哥", "新加坡", "土耳其", "泰国", "菲律宾"]
REGION_WEIGHT = {"美国": 3, "欧元区": 2, "中国": 2, "德国": 1, "英国": 1, "日本": 1}

# 关键指标标签：既用于加分，也用于跨源去重（同一天同地区同标签只保留一条）。
KEY_TAGS = [
    ("非农", r"非农就业|非农就业人口|非农就业人数"),
    ("失业率", r"失业率"),
    ("平均时薪", r"平均每小时工资"),
    ("CPI", r"CPI|消费者物价"),
    ("PCE", r"PCE"),
    ("PPI", r"PPI|生产者物价"),
    ("GDP", r"GDP|国内生产总值"),
    ("利率决议", r"利率决议|利率决定|议息"),
    ("ISM制造业", r"ISM制造业PMI"),
    ("ISM非制造业", r"ISM非制造业PMI"),
    ("ADP", r"ADP"),
    ("初请失业金", r"初请失业金"),
    ("零售销售", r"零售销售"),
    ("JOLTs", r"JOLTs|职位空缺"),
    ("消费者信心", r"消费者信心|密歇根"),
    ("耐用品订单", r"耐用品订单"),
    ("EIA原油库存", r"EIA原油库存"),
    ("挑战者裁员", r"挑战者企业裁员人数"),
    ("制造业PMI", r"制造业PMI|制造业采购经理"),
    ("服务业PMI", r"服务业PMI|非制造业PMI"),
]

# 同一时刻多条数据合并成一行时，用标签顺序决定谁当行首（非农优先于失业率）。
TAG_PRIORITY = {tag: len(KEY_TAGS) - index for index, (tag, _) in enumerate(KEY_TAGS)}

# 一线宏观指标：东方财富不给星级，用标签映射出与金十可比的重要性。
HIGH_TAGS = {"非农", "失业率", "CPI", "PCE", "利率决议", "ISM制造业", "ISM非制造业",
             "ADP", "初请失业金", "GDP", "零售销售", "PPI"}

# 管道类/分项类噪音：技术性拍卖、周度库存分项、调查分项等，永不进正文日历。
NOISE_RE = re.compile(
    r"竞拍|得标利率|投标倍数|配置百分比|红皮书|MBA|API|钻井|库欣|精炼|取暖油|成品油|汽油|"
    r"引伸需求|引申需求|蒸馏|新配方|外国央行持有|买进外国|外资买进|乳制品|四周均值|供应商交付|"
    r"物价支付|原材料支付|制成品价格|装船指数|外汇储备|外汇存底|基础货币|货币供应|"
    r"抵押贷款|营建许可|营建订单|新屋开工|建筑业PMI|政府预算|官方储备"
)
# ISM 只留制造业/非制造业总指数，分项（就业、新订单、库存、物价）全部剔除。
ISM_SUB_RE = re.compile(r"ISM(?!制造业PMI|非制造业PMI)")

# 事件类（会议/发布会）打分关键词
EVENT_HIGH_RE = re.compile(r"美联储|联储|欧洲央行|英国央行|日本央行|央行|议息|利率决议|"
                           r"OPEC|欧佩克|非农|CPI|财报|业绩|发布会|听证")
EVENT_ENTITY_RE = re.compile(r"英伟达|苹果|特斯拉|微软|谷歌|亚马逊|Meta|AMD|台积电|博通|"
                             r"OpenAI|甲骨文|美光|英特尔|超微|阿斯麦")


# ── 通用工具 ────────────────────────────────────────────


def now_bjt() -> dt.datetime:
    return dt.datetime.now(BRIEF_TZ).replace(microsecond=0)


def date_label(day: dt.date) -> str:
    """成稿日历日期列：9月4日 周五。"""
    return "%d月%d日 周%s" % (day.month, day.day, WEEKDAYS[day.weekday()])


def detect_region(title: str) -> str:
    for region in REGIONS:
        if title.startswith(region):
            return region
    for region in REGIONS:
        if region in title[:8]:
            return region
    return ""


def detect_tag(title: str) -> str:
    for tag, pattern in KEY_TAGS:
        if re.search(pattern, title):
            return tag
    return ""


def clean_title(title: str) -> str:
    """去掉单位括号、与日期列重复的“将于X月X日”，保留事件本名。"""
    title = re.sub(r"[（(][^）)]*[）)]", "", title or "")
    title = re.sub(r"将?于\d{1,2}月\d{1,2}日", "将", title)
    return re.sub(r"\s+", "", title).strip("·、,，")


def is_noise(title: str) -> bool:
    if NOISE_RE.search(title):
        return True
    if "ISM" in title and ISM_SUB_RE.search(title):
        return True
    return False


def score_data_item(title: str, star: int, region: str, tag: str) -> int:
    return int(star) + REGION_WEIGHT.get(region, 0) + (2 if tag else 0)


# ── 数据源 1：金十日历（本自然周经济数据 + 星级）───────────


def fetch_jin10_calendar() -> tuple[list[dict], list[str]]:
    items: list[dict] = []
    warnings: list[str] = []
    if not JIN10_MCP_TOKEN or jin10_call is None:
        warnings.append("未配置 JIN10_MCP_TOKEN，跳过金十日历（仅用东方财富）")
        return items, warnings
    payload = jin10_call("list_calendar", {})
    if not payload or payload.get("status") != 200:
        warnings.append("金十日历无返回，改用东方财富")
        return items, warnings
    for row in payload.get("data") or []:
        title = clean_title(row.get("title", ""))
        pub_time = str(row.get("pub_time") or "").strip()
        if not title or len(pub_time) < 16:
            continue
        try:
            when = dt.datetime.strptime(pub_time[:16], "%Y-%m-%d %H:%M").replace(tzinfo=BRIEF_TZ)
        except ValueError:
            continue
        if is_noise(title):
            continue
        region = detect_region(title)
        tag = detect_tag(title)
        star = int(row.get("star") or 1)
        items.append({
            "when": when,
            "title": title,
            "region": region,
            "tag": tag,
            "importance": star,
            "score": score_data_item(title, star, region, tag),
            "kind": "data",
            "source": "jin10-calendar",
            "consensus": row.get("consensus"),
            "previous": row.get("previous"),
        })
    return items, warnings


# ── 数据源 2：东方财富财经日历（事件 + 经济数据，跨周兜底）──


def _eastmoney_period(raw: str) -> str:
    m = re.search(r"报告期[:：](\d{4})年(\d{1,2})月", raw)
    if m:
        return "%d月" % int(m.group(2))
    m = re.search(r"报告期[:：](\d{4})年第?([一二三四1-4])季度", raw)
    if m:
        return "第%s季度" % m.group(2)
    return ""


def fetch_eastmoney_calendar(start: dt.date, end: dt.date) -> tuple[list[dict], list[str]]:
    items: list[dict] = []
    warnings: list[str] = []
    params = {
        "reportName": "RPT_CPH_FECALENDAR",
        "pageSize": 500,
        "sortColumns": "START_DATE",
        "sortTypes": 1,
        "filter": "(END_DATE>='%s')(START_DATE<'%s')" % (start.isoformat(), (end + dt.timedelta(days=1)).isoformat()),
        "source": "WEB",
        "client": "WEB",
        "columns": EASTMONEY_COLUMNS,
    }
    try:
        resp = requests.get(
            EASTMONEY_API, params=params, timeout=20,
            headers={"User-Agent": "Mozilla/5.0 (compatible; BriefBot/1.0)",
                     "Referer": "https://data.eastmoney.com/cjrl/"},
        )
        if resp.status_code != 200:
            warnings.append("东方财富日历 HTTP %d" % resp.status_code)
            return items, warnings
        data = resp.json()
    except (requests.RequestException, ValueError) as exc:
        warnings.append("东方财富日历请求失败：%s" % exc)
        return items, warnings
    if not data.get("success"):
        warnings.append("东方财富日历返回失败：%s" % data.get("message"))
        return items, warnings
    for row in (data.get("result") or {}).get("data") or []:
        raw_name = (row.get("FE_NAME") or "").strip()
        start_at = str(row.get("START_DATE") or "")
        if not raw_name or len(start_at) < 16:
            continue
        try:
            when = dt.datetime.strptime(start_at[:16], "%Y-%m-%d %H:%M").replace(tzinfo=BRIEF_TZ)
        except ValueError:
            continue
        type_code = str(row.get("STD_TYPE_CODE") or "")
        if type_code == "3":       # 新股申购，与本简报无关
            continue
        if type_code == "2":       # 经济数据："美国:非农就业人数:季调(报告期:2026年08月)"
            period = _eastmoney_period(raw_name)
            body = re.sub(r"[（(]报告期[:：][^）)]*[）)]", "", raw_name)
            parts = [p for p in re.split(r"[:：]", body) if p.strip()]
            region = parts[0].strip() if parts else ""
            core = parts[1].strip() if len(parts) > 1 else body
            title = clean_title("%s%s%s" % (region, period, core))
            if is_noise(title):
                continue
            region = detect_region(title)
            tag = detect_tag(title)
            star = 4 if tag in HIGH_TAGS else (3 if tag else 2)
            items.append({
                "when": when, "title": title, "region": region, "tag": tag,
                "importance": star, "score": score_data_item(title, star, region, tag),
                "kind": "data", "source": "eastmoney-fecalendar",
                "consensus": None, "previous": None,
            })
            continue
        # 事件类（会议、峰会、发布会等）
        title = clean_title(raw_name)
        if not title or is_noise(title):
            continue
        score = 5 + (2 if EVENT_HIGH_RE.search(title) else 0) + (2 if EVENT_ENTITY_RE.search(title) else 0)
        items.append({
            "when": when, "title": title[:40], "region": detect_region(title),
            "tag": detect_tag(title), "importance": 3 if score >= 7 else 2, "score": score,
            "kind": "event", "source": "eastmoney-fecalendar",
            "consensus": None, "previous": None,
        })
    return items, warnings


# ── 归并与筛选 ──────────────────────────────────────────


def dedupe(items: list[dict]) -> list[dict]:
    """跨源去重：同一天同地区同关键指标只留分高的一条；否则按标题去重。"""
    best: dict[tuple, dict] = {}
    for item in sorted(items, key=lambda x: (-x["score"], x["when"])):
        day = item["when"].date().isoformat()
        if item["tag"]:
            key = (day, item["region"], item["tag"])
        else:
            key = (day, re.sub(r"[^\w一-鿿]", "", item["title"])[:24])
        if key not in best:
            best[key] = item
    return list(best.values())


def group_events(items: list[dict]) -> list[dict]:
    """同一天同时刻同地区的数据合并成一行，避免非农/失业率/时薪各占一行。"""
    groups: dict[tuple, list[dict]] = {}
    for item in items:
        # 数据按“同一时刻同地区”合并；事件各自独立成行，避免把两件事拼成一句。
        key = (item["when"], item["region"], "data") if item["kind"] == "data" \
            else (item["when"], item["region"], "event", item["title"])
        groups.setdefault(key, []).append(item)
    merged = []
    for key, members in groups.items():
        when, region, kind = key[0], key[1], key[2]
        members.sort(key=lambda x: (-x["score"], -TAG_PRIORITY.get(x["tag"], 0), x["title"]))
        head = members[0]
        titles = [head["title"]]
        prefix = ""
        m = re.match(r"^(%s)(第[一二三四]季度|\d{1,2}月|至\d{1,2}月\d{1,2}日当周)?" % re.escape(region),
                     head["title"]) if region else None
        if m:
            prefix = m.group(0)
        for other in members[1:3] if kind == "data" else []:
            tail = other["title"]
            if prefix and tail.startswith(prefix):
                tail = tail[len(prefix):]
            if tail and tail not in titles[0]:
                titles.append(tail)
        text = "、".join(t for t in titles if t)
        if kind == "data" and when.strftime("%H:%M") != "00:00":
            event = "%s %s" % (when.strftime("%H:%M"), text)
        else:
            event = text
        merged.append({
            "when": when,
            "event": event[:46],
            "region": region,
            "tag": head["tag"],
            "kind": kind,
            "importance": max(int(x["importance"]) for x in members),
            "score": head["score"],
            "source": head["source"],
        })
    return merged


def select_events(items: list[dict], max_count: int, min_count: int) -> tuple[list[dict], list[str]]:
    warnings: list[str] = []
    chosen: list[dict] = []
    used_threshold = SCORE_THRESHOLDS[0]
    for threshold in SCORE_THRESHOLDS:
        chosen = [x for x in items if x["score"] >= threshold]
        used_threshold = threshold
        if len(chosen) >= min_count:
            break
    if used_threshold != SCORE_THRESHOLDS[0]:
        warnings.append("重要事件不足，重要性阈值放宽到 %d" % used_threshold)
    chosen.sort(key=lambda x: (-x["score"], x["when"]))
    chosen = chosen[:max_count]
    chosen.sort(key=lambda x: x["when"])
    return chosen, warnings


def stars_of(importance: int) -> int:
    """成稿星级统一压到 1-3 档，保证表格宽度稳定。"""
    if importance >= 4:
        return 3
    if importance == 3:
        return 2
    return 1


def build_feed(days: int, max_count: int, min_count: int, now: dt.datetime | None = None) -> dict:
    now = now or now_bjt()
    window_start = now
    window_end = now + dt.timedelta(days=days)

    raw, warnings = fetch_jin10_calendar()
    jin10_count = len(raw)
    em_items, em_warnings = fetch_eastmoney_calendar(now.date(), window_end.date())
    warnings += em_warnings
    raw += em_items

    in_window = [x for x in raw if window_start <= x["when"] <= window_end]
    picked, pick_warnings = select_events(group_events(dedupe(in_window)), max_count, min_count)
    warnings += pick_warnings
    if not picked:
        warnings.append("窗口内没有任何可用日历事件")

    events = []
    for item in picked:
        day = item["when"].date()
        events.append({
            "date_iso": day.isoformat(),
            "date": date_label(day),
            "time": item["when"].strftime("%H:%M"),
            "event": item["event"],
            "stars": stars_of(item["importance"]),
            "importance": item["importance"],
            "kind": item["kind"],
            "source": item["source"],
        })
    return {
        "schema_version": SCHEMA_VERSION,
        "fetched_at": now.isoformat(),
        "window_start": window_start.isoformat(),
        "window_end": window_end.isoformat(),
        "sources": {"jin10-calendar": jin10_count, "eastmoney-fecalendar": len(em_items)},
        "candidate_count": len(in_window),
        "warnings": warnings,
        "events": events,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="抓取本周财经日历，写出 calendar_feed.json")
    parser.add_argument("--days", type=int, default=WINDOW_DAYS, help="展望自然日天数（默认 7）")
    parser.add_argument("--max", dest="max_count", type=int, default=MAX_EVENTS, help="最多保留几条（默认 5）")
    parser.add_argument("--min", dest="min_count", type=int, default=MIN_EVENTS, help="低于该条数则放宽阈值（默认 3）")
    parser.add_argument("--out", default=str(OUT_PATH), help="输出路径")
    args = parser.parse_args()

    feed = build_feed(args.days, args.max_count, args.min_count)
    Path(args.out).write_text(json.dumps(feed, ensure_ascii=False, indent=2), encoding="utf-8")
    for warning in feed["warnings"]:
        print("[warn] %s" % warning, file=sys.stderr)
    print("已写入: %s（%d 条，候选 %d，金十 %d / 东财 %d）" % (
        args.out, len(feed["events"]), feed["candidate_count"],
        feed["sources"]["jin10-calendar"], feed["sources"]["eastmoney-fecalendar"]), file=sys.stderr)
    for event in feed["events"]:
        print("  %s %s %s" % (event["date"], "⭐" * event["stars"], event["event"]), file=sys.stderr)
    return 0 if feed["events"] else 1


if __name__ == "__main__":
    sys.exit(main())
