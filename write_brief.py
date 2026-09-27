#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""DeepSeek 直连写稿器：根据行情快照 + 聚合新闻池 + 模板，生成 content_YYYYMMDD.json。

服务器上替代 headless Claude Code 的写稿环节（无 Anthropic key 时）。
流程：读 market_snapshot.json（as_of/行情）→ 过滤 news_feed.json 到 24h 窗口 →
构造提示词调 DeepSeek API → 解析/清洗 JSON（修正 as_of、日期、来源时效）→
写 content_YYYYMMDD.json → 跑 build_brief.py 校验字数 → 携带上一版 JSON 定向修复（最多6次）。

环境变量：
    DEEPSEEK_API_KEY  必需，DeepSeek API key（服务器写入 .secrets.env，chmod 600）

用法：
    .venv/bin/python write_brief.py                 # 按 market_snapshot.run_started_at 日期
    .venv/bin/python write_brief.py 20260806        # 指定日期
    .venv/bin/python write_brief.py --views views.txt   # 附带飞书观点文本
"""

from __future__ import annotations

import datetime as dt
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

from content_validation import (CALENDAR_REQUIRED_FROM, ensure_emphasis, trim_long_emphasis,
                                validate_calendar, validate_emphasis, validate_no_structure_leaks)

BASE = Path(__file__).parent
BRIEF_TZ = ZoneInfo("Asia/Shanghai")
API_URL = "https://api.deepseek.com/chat/completions"
MODEL = os.environ.get("DEEPSEEK_MODEL", "deepseek-chat")
MAX_ATTEMPTS = 6
TARGET_MIN, TARGET_MAX = 6300, 6600
HARD_MIN, HARD_MAX = 6000, 7000
WEEKDAYS = ("周一", "周二", "周三", "周四", "周五", "周六", "周日")
NEWS_CAP = 100
# 新闻池来源分级：1=海外一手媒体（优先入池）、2=国内实时快讯、3=国内聚合热榜；未登记来源按 3 级。
# 海外一手最多占 OVERSEAS_CAP 席，为 A 股/国内视角保留余量；二、三级不足时再由剩余海外条目补满。
SOURCE_TIERS = {
    "bloomberg": 1, "reuters": 1, "cnbc": 1,
    "futu-news-search": 2, "jin10-flash": 2, "jin10-news": 2, "wscn-live": 2,
    "cls": 3, "wallstreetcn": 3, "xueqiu": 3,
}
OVERSEAS_CAP = 60
TITLE_LINE_MAX = 24
ALERT_MIN, ALERT_MAX = 180, 260
NOTE_MIN, NOTE_MAX = 30, 100
TITLE_FORBIDDEN_PHRASES = (
    "蓄势", "主线浮现", "机会浮现", "布局良机", "布局机会", "买点",
    "看多", "看空", "受益方向", "静待方向", "静待突破", "变盘在即",
    "风云再起", "谁主沉浮", "狂欢开启", "利好", "利空",
)
TITLE_FACT_VERBS = (
    "发布", "宣布", "上调", "下调", "加码", "入股", "收购", "增持", "减持",
    "收涨", "收跌", "上涨", "下跌", "大涨", "大跌", "暴涨", "暴跌", "飙涨", "飙升",
    "反弹", "回落", "领涨", "领跌", "逆袭", "走高", "走低", "走强", "走弱", "持稳",
    "突破", "站上", "失守", "跌破", "创新高", "创新低",
    "升温", "降温", "拦截", "封锁", "达成", "终止", "重启", "扩大", "收窄",
    "增长", "下降", "上升", "降息", "加息", "维持", "入选", "获批", "推出",
    "翻倍", "跳涨", "攀升", "上扬", "下挫", "重挫", "企稳", "签署", "完成",
    "恢复", "暂停", "澄清", "改组", "离职", "上市", "落地", "揭晓", "磋商",
    "涨", "跌",
)
# 旧版高质量简报的三条逻辑线：科技主线（紫）、宏观逆风（红）、独立变量（绿）。
PATH_COLORS = ["#534AB7", "#A32D2D", "#0F6E56"]

# 少而完整的“论证段”结构，避免把同一主题切成大量模板化小段。
BULLET_RULES = {
    "focus": (3, 4),
    "ai_tech": (4, 5),
    "stock_rotation": (2, 3),
    "iran": (3, 3),
    "macro": (3, 4),
    "commodity": (2, 2),
}
# 单段硬底线只拦截失去论证性的碎段；更高的目标长度由提示词与整稿
# 6000-7000 字硬校验共同约束。否则模型会把全部重试浪费在补十几个字，
# 反而没有机会修正真正重要的整稿篇幅。
MIN_BULLET_CHARS = {
    "focus": 100,
    "ai_tech": 100,
    "stock_rotation": 100,
    "iran": 100,
    "macro": 100,
    "commodity": 100,
}
# ── 淡日感知 + 降级放行档位 ──────────────────────────────
# 正常档沿用上面的常量。淡日=美股休市/长假后首日（隔夜无新成交、过夜新闻偏少），
# 此时放宽整稿字数下限与 ai_tech 条数下限，避免模型为凑字注水或整篇被判死。
# ACCEPT_MIN：降级放行下限——结构/时效/日历/强调色全过、build rc=0，仅整稿略短于硬下限时接受。
ACCEPT_MIN = 5500

NORMAL_PROFILE = {
    "thin": False,
    "target_min": TARGET_MIN, "target_max": TARGET_MAX,
    "hard_min": HARD_MIN, "hard_max": HARD_MAX,
    "accept_min": ACCEPT_MIN,
    "bullet_rules": BULLET_RULES,
    "min_bullet_chars": MIN_BULLET_CHARS,
}
THIN_PROFILE = {
    "thin": True,
    "target_min": 5800, "target_max": 6300,
    "hard_min": 5500, "hard_max": HARD_MAX,
    "accept_min": 5200,
    "bullet_rules": {**BULLET_RULES, "ai_tech": (3, 5)},
    "min_bullet_chars": {sid: 90 for sid in MIN_BULLET_CHARS},
}
NOTE_FLOOR_SUFFIXES = {
    "focus": "后续重点观察关键事件、数据与盘面能否形成同向验证。",
    "ai_tech": "后续重点观察订单、资本开支与业绩指引能否继续验证。",
    "stock_rotation": "后续关注成交、利率与领涨结构能否确认轮动级别。",
    "iran": "后续以谈判进展、通航状态与油价反应作为验证。",
    "macro": "后续重点观察数据、收益率与政策表态是否同向。",
    "commodity": "后续重点观察实际利率、美元与油价是否形成共振。",
}
# bullet 差几个字的碎段兜底：在段末追加一句板块专属验证条件（不引入新事实/数字），
# 与 NOTE_FLOOR_SUFFIXES 同理。措辞与 note 版不同，避免同段落尾句雷同。
BULLET_FLOOR_SUFFIXES = {
    "focus": "这一分化能否延续，仍取决于后续盘面与数据的同向验证。",
    "ai_tech": "订单、资本开支与业绩指引能否兑现，是后续的关键验证点。",
    "stock_rotation": "轮动能否升级，仍取决于成交、利率与领涨结构的确认。",
    "iran": "后续仍需以谈判进展、通航状态与油价反应作为验证。",
    "macro": "数据、收益率与政策表态是否同向，将决定下一步定价。",
    "commodity": "实际利率、美元与油价能否形成共振，仍需持续跟踪。",
}
DEFAULT_BULLET_SUFFIX = "后续仍需持续跟踪相关数据与盘面能否相互验证。"
ALERT_FLOOR_SUFFIX = "后续以关键数据、利率与盘面反应作为验证。"

# ── 超限侧确定性压缩 ──────────────────────────────────────
# 对称于偏短侧的 ensure_length_floors / 超长着色的 trim_long_emphasis：
# 模型在硬上限附近反复微调、连续 6 次收敛不进区间时（2026-09-16 顽疾，与
# 09-08/09-09 同源，只是发生在上边界），作为最后兜底删除板块 bullet 中
# “既无数字也无强调色”的纯衔接/解读冗余句，把整稿压回硬区间内部。
# 只保留每条首句(主题)与全部含数字/强调色的句子，可见正文里的事实、数字、
# 机构观点与多空方向因此原样保留，仅去掉重复背景与软性过渡。
COMPRESS_MARGIN = 40        # 压到 hard_max - 余量，落在区间内部而非贴边
OVERSHOOT_TOLERANCE = 400   # 仅当超出硬上限 <= 该值才自动压缩，过多则交人工复核


def now_bjt() -> dt.datetime:
    return dt.datetime.now(BRIEF_TZ).replace(microsecond=0)


def parse_iso(value) -> dt.datetime | None:
    if not value:
        return None
    try:
        return dt.datetime.fromisoformat(str(value)).astimezone(BRIEF_TZ)
    except (TypeError, ValueError):
        return None


def load_snapshot() -> dict:
    path = BASE / "market_snapshot.json"
    if not path.exists():
        sys.exit("✗ 缺 market_snapshot.json —— 先运行 fetch_market.py")
    snap = json.loads(path.read_text(encoding="utf-8"))
    if snap.get("valid") is not True or snap.get("schema_version") != 2:
        sys.exit("✗ 行情快照无效，禁止写稿")
    return snap


def source_tier(source_id: str) -> int:
    return SOURCE_TIERS.get(source_id or "", 3)


def build_news_pool(window_start: dt.datetime, window_end: dt.datetime) -> list[dict]:
    """读 news_feed.json，过滤到 24h 窗口、去重，按来源分级截断，返回候选池。

    海外一手媒体（Bloomberg/Reuters/CNBC）优先入池、最多 OVERSEAS_CAP 条；其余席位依次给
    国内实时快讯、国内热榜；仍有空位时再用剩余海外条目补满。池内按（级别, 发布时间倒序）排列。"""
    path = BASE / "news_feed.json"
    if not path.exists():
        sys.exit("✗ 缺 news_feed.json —— 先运行 fetch_news.py")
    feed = json.loads(path.read_text(encoding="utf-8"))
    candidates = []
    for item in feed.get("items", []):
        published = parse_iso(item.get("published_at"))
        if not published or not (window_start <= published <= window_end):
            continue
        source_id = item.get("source_id") or ""
        candidates.append({
            "source_name": item.get("source_name") or item.get("source", "未知"),
            "source_id": source_id,
            "tier": source_tier(source_id),
            "title": (item.get("title") or "").strip()[:160],
            "summary": (item.get("summary") or "").strip()[:220],
            "published_at": published.isoformat(),
            "_ts": published.timestamp(),
        })
    candidates.sort(key=lambda it: (it["tier"], -it["_ts"]))
    seen, unique = set(), []
    for item in candidates:
        key = re.sub(r"\s+", "", item["title"])[:80]
        if key in seen:
            continue
        seen.add(key)
        unique.append(item)
    overseas = [it for it in unique if it["tier"] == 1]
    domestic = [it for it in unique if it["tier"] != 1]
    pool = overseas[:OVERSEAS_CAP]
    pool += domestic[:NEWS_CAP - len(pool)]
    pool += overseas[OVERSEAS_CAP:][:NEWS_CAP - len(pool)]
    pool.sort(key=lambda it: (it["tier"], -it["_ts"]))
    for item in pool:
        del item["_ts"]
    return pool


CALENDAR_FEED_MAX_AGE_H = 12


def load_calendar_feed() -> tuple[list[dict], list[str]]:
    """读 fetch_calendar.py 产出的 calendar_feed.json。

    日历条目由结构化日历源确定，模型不再决定日期；文件缺失/过期时返回空列表，
    由调用方决定是告警还是终止。
    """
    path = BASE / "calendar_feed.json"
    if not path.exists():
        return [], ["缺 calendar_feed.json —— 先运行 fetch_calendar.py"]
    try:
        feed = json.loads(path.read_text(encoding="utf-8"))
    except ValueError as exc:
        return [], ["calendar_feed.json 解析失败：%s" % exc]
    if feed.get("schema_version") != 1:
        return [], ["calendar_feed.json 版本不符，需重新运行 fetch_calendar.py"]
    fetched_at = parse_iso(feed.get("fetched_at"))
    if not fetched_at:
        return [], ["calendar_feed.json 缺 fetched_at"]
    age_h = (now_bjt() - fetched_at).total_seconds() / 3600
    if age_h > CALENDAR_FEED_MAX_AGE_H:
        return [], ["calendar_feed.json 已过期(%.1f 小时前) —— 重新运行 fetch_calendar.py" % age_h]
    events = [e for e in feed.get("events", []) if isinstance(e, dict) and e.get("date") and e.get("event")]
    return events, []


def format_calendar_feed(events: list[dict]) -> str:
    lines = []
    for event in events:
        lines.append("- %s %s %s（重要性 %s）" % (
            event.get("date", ""), event.get("time", ""), event.get("event", ""),
            "⭐" * int(event.get("stars", 1) or 1)))
    return "\n".join(lines)


def apply_calendar_feed(content: dict, events: list[dict]) -> list[str]:
    """用确定性日历覆盖模型输出的 calendar，杜绝空表与编造日期。"""
    if not events:
        return []
    content["calendar"] = [{
        "date": event["date"],
        "date_iso": event.get("date_iso", ""),
        "event": event["event"],
        "stars": int(event.get("stars", 1) or 1),
    } for event in events]
    return ["本周财经日历由 calendar_feed.json 确定性回填 %d 条" % len(events)]


def format_market(snap: dict) -> str:
    lines = [snap.get("index_date_label", ""), snap.get("index_price_label", "")]
    lines.append("指数:")
    for row in snap.get("indices", []):
        lines.append("  %s %s %s" % (row["name"], row["price"], row["chg"]))
    lines.append("大宗/外汇:")
    for row in snap.get("commodities", []):
        lines.append("  %s %s %s" % (row["name"], row["price"], row["chg"]))
    return "\n".join(lines)


def _expected_prev_us_session(brief_date: dt.date) -> dt.date:
    """给定简报日期，返回“正常情况下”最近的一个美股交易日（周末回退到周五）。
    仅用星期数学，不依赖外部交易日历——足以把长假/临时休市识别为“最近交易日更旧”。"""
    weekday = brief_date.weekday()  # 周一=0 … 周日=6
    if weekday == 0:      # 周一 → 上周五
        delta = 3
    elif weekday == 5:    # 周六 → 周五
        delta = 1
    elif weekday == 6:    # 周日 → 周五
        delta = 2
    else:                 # 周二至周五 → 前一天
        delta = 1
    return brief_date - dt.timedelta(days=delta)


def is_thin_session(snap: dict, brief_date: dt.date) -> bool:
    """淡日判定：美股最近交易日比“应有的上一交易日”更旧，说明中间有休市日
    （长假/节日），隔夜无新成交、过夜新闻偏薄。每天早晨美股都处于“休市”状态，
    因此不能用 market_state，而要看 session_date 是否落后于预期。"""
    session_date = snap.get("price_index_session_date")
    if not session_date:
        return False
    try:
        session = dt.date.fromisoformat(str(session_date))
    except ValueError:
        return False
    return session < _expected_prev_us_session(brief_date)


def build_prompt(snap: dict, pool: list[dict], day: str, date_cn: str, alert_time: str,
                 as_of: str, index_note: str, qdii_label: str, views_text: str,
                 feedback: str | None = None, calendar_events: list[dict] | None = None,
                 profile: dict | None = None) -> str:
    profile = profile or NORMAL_PROFILE
    tmin, tmax = profile["target_min"], profile["target_max"]
    hmin, hmax = profile["hard_min"], profile["hard_max"]
    thin_block = ("\n【淡日提示（重要）】今日为美股休市/长假后的第一个交易日：隔夜美股无新成交、"
                  "过夜新闻明显偏少。请勿为凑字硬造事实、堆砌碎段或重复背景；把篇幅重心放在宏观利率、"
                  "大宗商品、亚洲与欧洲盘面，以及【本周财经日历】的前瞻解读上。允许每条论证段略短、"
                  "板块条数取下限，整体字数下限已相应放宽——写满真实信息即可，不要注水。\n"
                  ) if profile.get("thin") else ""
    sections_hint = (
        "固定板块顺序：focus(头号焦点)、ai_tech(AI·芯片·科技，核心板块)、"
        "stock_rotation(资金轮动)、iran(中东地缘，仅当有实质进展时)、macro(宏观·利率)、"
        "commodity(大宗·外汇，固定用两段覆盖黄金，以及布伦特原油期货+美元指数)。\n"
        "每个板块对象包含 id/title/bullets/note。目标结构为 focus 3 条、ai_tech 4 条、"
        "stock_rotation 2 条、iran 3 条、macro 3 条、commodity 2 条；无 iran 时 focus 与 ai_tech "
        "可各增加 1 条。每条 bullet 是一个完整的主题论证段，不是资讯卡片。不要机械地给每段加"
        "**粗体小标题**；只在确有必要时用 [[c:结论短句]] 领起。每条 bullet 必须至少有 1 处"
        "强调色，focus 与 ai_tech 平均每条至少 2 处；用 [[red:上涨/改善/利多信号]]、"
        "[[green:下跌/恶化/利空信号]]、[[c:关键事实或判断]] 标记。强调色只包住 4-40 字的"
        "数字或短语，不得包整段；**粗体**不计入强调色。note 用一到两句压缩本节真正的交易结论。"
    )
    news_lines = []
    for i, item in enumerate(pool, 1):
        text = item["title"] + ("。" + item["summary"] if item["summary"] else "")
        news_lines.append("[%d] %s | %s | %s" % (i, item["published_at"], item["source_name"], text))
    news_block = "\n".join(news_lines)

    views_block = ("\n【你的观点参考】（仅方向观点清单：各主题的多空判断与关注要点。"
                   "**只用于解读/点评的方向对齐，绝不是新闻事实来源，严禁直接引用其标题、措辞、数字或事件表述**）\n"
                   + views_text.strip()) if views_text.strip() else ""
    feedback_block = ("\n【上一次反馈】\n" + feedback + "\n必须针对该反馈修正后重新完整输出。") if feedback else ""
    calendar_block = ("\n【本周财经日历】（结构化日历源，日期与时间以此为准；"
                      "正文提到未来数据/事件时只能引用这里的日期，calendar 字段由脚本回填）\n"
                      + format_calendar_feed(calendar_events)) if calendar_events else ""
    return f"""你是一名财经简报主笔。请根据下面提供的【行情数据】【新闻池】【你的观点参考】【结构要求】写一份当天的全球财经简报，并把结果输出为**一个完整的 JSON 对象**（不要输出任何额外文字、解释或 markdown 代码块）。

【关键约束】
1. **事实与解读分离**：对新闻事实（谁、发生了什么、官方数据、公司财报数字）严格按【新闻池】与【行情数据】客观呈现，原样引用数字，不套用任何主观立场。
2. **新闻内容来源红线**：正文所有新闻事实、数据、事件、人物言论、公司财报数字，**只能来自【新闻池】与【行情数据】**。【你的观点参考】仅提供多空方向，**严禁**把它当作素材——不得引用其标题、措辞、价格/持仓数字或事件表述作为正文内容，也**严禁**把飞书标题或措辞改写成简报标题、alert、板块标题。**正文严禁出现"据XX报道""据报道""有报道称""据报""据XX称"等来源转述或出处表述**——事实直接客观陈述，不标注、不转述新闻来源（来源登记只存在于 JSON 的 sources 字段，由脚本自动挂接）。
3. **解读仅对齐方向**：解读/点评/路径/风险判断的**多空方向**必须与【你的观点参考】一致（最新观点优先），但要用市场通用的专业语言独立表达，不得照搬原文句子。
4. 人名简称：「习近平」一律写作「习」（如“国家主席习”“习主席”）。客户成稿只写事实、公开数据与投资解读，禁止出现"行情快照""脚本""新闻池""fetch""project""观点参考"等内部机制词；解读用"课代表框架/视角"，不要出现"用户视角"。
5. 输出 JSON 必须包含字段：date_yyyymmdd、date_cn、as_of、main_title、alert_time、alert、index_note、qdii_date_label、logic_chain、calendar、sources、sections。**禁止**写 indices/commodities 字段。
6. **main_title 必须是高信息密度的两行新闻标题（重要）**：标题要让读者一眼看到当天最重要的交易结构，不得写成投资观点、行情预测或空泛研报摘要。
   - **结构**：优先写成两行；第一行放主事件及直接市场反应，第二行放另一条足以影响定价的科技、宏观、地缘或大宗副线。每行可容纳 1-2 个紧密相关的事实信号，但不得堆砌无关主题。只有当天确实只有一条核心增量时才用单行。
   - **主体与动词**：主体必须具体（公司/指数/央行/国家地区/资产），不能只写“市场/资金/情绪”。优先用“发布/宣布/上调/下调/加码/入股/收涨/收跌/反弹/回落/突破/升温”等已发生、可验证的动词。
   - **事实边界**：事件、数字、方向和因果都必须能在【新闻池】或【行情数据】逐项核对。只有新闻原文明确建立因果时才能写“因……上涨/下跌”；否则并列陈述，不自行归因。
   - **禁止写法**：不用问句、感叹号、口号、隐喻、悬念或标题党；不得使用“蓄势/主线浮现/机会浮现/布局良机/买点/看多/看空/利好/利空/受益方向/静待方向/静待突破/变盘在即/风云再起/谁主沉浮/狂欢开启”等观点、预测或情绪词；不得出现来源转述、内部取数机制或飞书文章标题/改写。
   - **格式**：最多两行，用 `<br>` 分隔，每行最多 **24 个字符**（数字和标点均计数）；不要为了短而删除关键主体、数字或第二条定价主线。
   - **正反例**：合格“存储飙涨领衔·全球科技反弹<br>油价上行·黄金守住关键位”“英伟达扩建算力·费半反弹<br>美债收益率回落·布油上涨”；不合格“科技回调黄金蓄势”（观点+模糊预测）“AI主线机会浮现”（投资结论）“油金谁主沉浮？”（悬念问句）。
   alert 为"**过去24小时核心变化**：..."开头的短导读，控制在 180-240 字，只交代科技/市场、宏观/地缘和一句交易结构，禁止把正文再复述一遍；alert 必须有至少 2 处强调色。logic_chain 含 paths 三项：路径A写当天科技/产业主线，路径B写宏观或利率逆风，路径C写地缘/黄金/其他独立变量；每项含 name/color/body/sub，body 用 <br> 分隔短句，名称必须随当天事实变化；**三路径 color 固定为：路径A=#534AB7(紫) 路径B=#A32D2D(红) 路径C=#0F6E56(绿)**。calendar 字段输出空数组 []，本周财经日历由脚本按结构化日历源确定性回填；正文若要提到未来事件，日期必须与【本周财经日历】一致。
7. **板块结构（schema 必须严格遵守）**：{sections_hint}
   板块内容规范：
   - `focus`：3 条完整论证段，依次完成 ①核心事实与盘面 ②跨市场/产业交叉验证 ③课代表框架、反方信号与验证条件；无 iran 时可增至 4 条。
   - `ai_tech`（核心板块，字数最多）：4 条完整论证段，按当天信息自然组合算力/芯片、AI前沿、云资本支出、政策生态及存储/CPO/PCB/电力冷却等“卖铲子”环节；不要为了覆盖目录而硬凑空段。无 iran 时可增至 5 条。
   - `stock_rotation`：2 条，第一条讲市场风格与资金去向，第二条讲科技以外最有独立催化的板块/公司；确有第三条强信号时可增至 3 条。
   - `iran`（地缘）：条件板块，无实质进展（战事/停火/制裁重大变化）就移除；有内容时固定 3 条，分别讲事实进展、通航/供应验证、油价→通胀→利率传导。
   - `macro`：3 条，围绕利率定价、政策/数据、后续验证窗口组成一条因果链；重大宏观日可增至 4 条。
   - `commodity`：固定 2 条；第一条黄金，第二条合并布伦特原油期货与美元指数，以三者的利率/地缘传导关系收束。
8. **海外投行研报（加分铁律）**：在 `ai_tech`/`stock_rotation`/`macro` 附 1-2 条当日市场热议的海外投行/机构研报解读，**必须点名机构**（高盛/摩根士丹利/摩根大通/美银/花旗/Rosenblatt/Wedbush/巴克莱等），如评级/目标价调整、行业规模预测、多空研判；只列热门研报，冷门不列；观点客观呈现（谁说了什么），解读走课代表框架。**新闻池没有对应研报内容时不得虚构机构名与数字。**
9. **主线判断与解读框架（对齐项目聚焦方向）**：
   - AI 上游=超级产业周期（设备/材料/存储/CPO/PCB/电力冷却等"卖铲子"环节是主线），科技回调=交易层面（拥挤度/涨太高）而非基本面逆转；下游应用/老登板块反弹=日线级高切低，难成主线。
   - 黄金由利率预期主导：地缘经"油价→通胀→利率"链条影响黄金（霍尔木兹关闭→油↑→通胀↑→加息预期→实际利率↑→黄金承压；开放→黄金受益），**禁止写"封锁海峡→黄金上行触发器"**这类链条。
   - 美联储路径：按【新闻池】里的最新政策动作与市场定价设基准/偏好/偏坏三种情景（如已进入加息周期则围绕加息节奏展开），不固定套用“不降息/降1次/加1次”；油价回落→CPI降温→加息预期修正。
10. **时效铁律**：以最近 12 小时为主体、24 小时为上限；**已公布数据（CPI/PPI/NFP 等）不得作为头条/alert 的"最新"事件**，只能作背景引用或列入 calendar 的待发布项；宏观板块须以当日最新角度开篇（利率定价/官员表态/当日数据/前瞻窗口）。**未来日期严禁凭记忆填写**：正文提到的未来数据/事件日期只能来自【本周财经日历】或【新闻池】原文；calendar 字段留空数组，由脚本用结构化日历源回填。
11. **海外一手媒体优先**：新闻池中 Bloomberg/Reuters/CNBC 为海外一手媒体（英文原文须准确译成中文，公司名、人名、机构名用通行中文译名）。同一事件多源报道时，事实表述、数字、时间以海外一手媒体为准；国内快讯/热榜与之冲突时采用海外一手版本，国内来源只补充 A 股/港股/国内政策视角。来源名仅用于核对，正文仍不得出现来源转述。
12. **头号焦点优先级**：AI大模型发布/重大突破 > 芯片/半导体重大事件 > 科技巨头财报 > CEO重磅发言 > 宏观政策 > 地缘（仅战争升级/停火/制裁重大变化级别）。
13. 字数（最重要）：渲染后正文目标 **{tmin}-{tmax} 字**，硬范围 {hmin}-{hmax}。不要靠增加碎段凑字。**focus 每段 280-360 字，ai_tech 每段 270-350 字，stock_rotation/iran/macro 每段 230-310 字，commodity 两段各 250-320 字**；这里指去掉 `[[c:]]`、`[[red:]]`、`[[green:]]` 和 `**` 后读者实际看到的字数。每段通常 3-5 句，内部顺序为“事实与数字→市场反应/交叉验证→传导逻辑→风险或验证条件”。alert 可见正文 180-240 字，logic_chain 合计 450-650 字，note 40-90 字。无 iran 时，把缺口集中补进 focus 与 ai_tech 的新增论证段。
14. **编辑感铁律**：板块标题必须写出本节核心矛盾或结论，格式为“板块名 | 结论/反差”，不能只是罗列新闻名词；段落之间用事实推进。同一事实只在最相关板块完整展开，其他板块只写其新增含义，禁止重复背景、万能结论和“短线波动不改长期趋势”式套话。
{thin_block}
【日期与标识】（照抄以下值）
- date_yyyymmdd = "{day}"
- date_cn = "{date_cn}"
- as_of = "{as_of}"
- alert_time = "{alert_time}"
- index_note = "{index_note}"
- qdii_date_label = "{qdii_label}"

【行情数据】
{format_market(snap)}

【新闻池】（共 {len(pool)} 条，均落在 {snap["news_window"]["start"]} 至 {snap["news_window"]["end"]} 窗口内；每行格式为「序号 发布时间 | 来源 | 标题。摘要」，按海外一手媒体→国内快讯→国内热榜排序）
{news_block}
{calendar_block}
{views_block}
{feedback_block}"""


def build_repair_prompt(content: dict, feedback: str, pool: list[dict], day: str,
                        date_cn: str, alert_time: str, as_of: str, index_note: str,
                        qdii_label: str, profile: dict | None = None) -> str:
    """Build a focused revision request around the last parseable candidate."""
    profile = profile or NORMAL_PROFILE
    tmin, tmax = profile["target_min"], profile["target_max"]
    hmin, hmax = profile["hard_min"], profile["hard_max"]
    news_lines = []
    for i, item in enumerate(pool[:30], 1):
        text = item["title"] + ("。" + item["summary"] if item["summary"] else "")
        news_lines.append("[%d] %s | %s" % (i, item["published_at"], text))
    candidate = json.dumps(content, ensure_ascii=False, indent=2)
    return f"""你正在修订一份已有的财经简报 JSON。请根据【校验反馈】定向修复【上一版 JSON】，只输出修复后的一个完整 JSON 对象，不要输出解释或 markdown。

【修订原则】
1. 保留上一版中已正确的内容和事实，只修改校验反馈指向的字段；不要无关重写整篇。
2. 若反馈指出某个 alert/bullet/note 低于最低字数，必须保留该字段现有文字不动，只在末尾追加 1-3 句已有事实的传导含义、反方风险或验证条件，写到最低值以上再额外留 10-20 字余量；禁止同义改写后长度不变，也禁止缩短其他合格字段。不得编造新事实、新数字或机构观点。
3. 字数超限时，按反馈中的分段字数优先压缩最大板块，删除重复解读，保留事实、关键数字和结论。
4. sections 顺序必须是 focus、ai_tech、stock_rotation、iran（仅有实质进展时）、macro、commodity。目标为 focus 3 条、ai_tech 4 条、stock_rotation 2 条、iran 3 条、macro 3 条、commodity 2 条；无 iran 时 focus/ai_tech 可各增加 1 条，重大信号充足时 stock_rotation/macro 可各增加 1 条。每个板块必须有 id/title/bullets/note。
5. logic_chain 必须有 paths 三项和非空 priority，每条路径必须有 name/color/body/sub；priority 必须是单个字符串，不能是数组。calendar 由脚本确定性回填，保持原样不要改动。
6. sections+alert+logic_chain+calendar 最终必须在 {hmin}-{hmax} 字，优先收敛到 {tmin}-{tmax} 字。
7. 正文事实只能来自下方新闻池或上一版已有内容；禁止出现“据XX报道”“据报道”“有报道称”、“新闻池”、“行情快照”、“脚本”等客户不可见表述。
8. 以下标识必须原样使用：date_yyyymmdd={day}；date_cn={date_cn}；as_of={as_of}；alert_time={alert_time}；index_note={index_note}；qdii_date_label={qdii_label}。禁止输出 indices/commodities。
9. 若反馈涉及强调色，只在原有事实中圈选短语加标记，不改写或新增事实：每条 bullet 至少 1 处，focus/ai_tech 合计至少为 bullet 数的 2 倍，alert 至少 2 处；red=上涨/改善/利多，green=下跌/恶化/利空，c=关键事实/机构观点/结论/验证条件；单处不超过 40 字，禁止整段着色，**粗体**不计数。

【校验反馈】
{feedback}

【上一版 JSON】
{candidate}

【可用新闻池】
{chr(10).join(news_lines)}
"""


def extract_stance(views_text: str) -> str:
    """把飞书文章提炼成【立场清单】：只保留各主题的多空方向与关注要点，
    剥离标题、措辞、具体数字与事件细节，防止飞书素材被当作简报内容引用。
    失败时返回空串（调用方回退到不参考观点）。"""
    prompt = (
        "以下是投资者『股市马斯克』最近 3 篇飞书文章。请提炼成一个【立场清单】，"
        "供一份财经简报的解读部分对齐方向。要求：\n"
        "- 只输出各主题的多空方向判断与关注要点（如：黄金看多/看空/中性、原油、美元、"
        "美联储加息预期、AI/半导体、存储、中东地缘、中国股市等）；\n"
        "- 每个主题一行，'主题：方向（一句话要点）'，共 6-12 行；\n"
        "- 严禁照搬原文标题、措辞、具体价格/百分比/事件细节、持仓数字；\n"
        "- 只输出清单本身，不要任何其他文字。\n\n文章如下：\n" + views_text[:36000]
    )
    raw, _ = call_deepseek(prompt)
    raw = re.sub(r"```(?:json)?\s*", "", raw).strip()
    lines = [
        l.strip().strip('"').strip().rstrip(",").strip()
        for l in raw.splitlines()
        if l.strip() and l.strip() not in ("[", "]", "```", "```json")
    ]
    return "\n".join(lines)[:4000]


def call_deepseek(prompt: str) -> str:
    key = os.environ.get("DEEPSEEK_API_KEY", "").strip()
    if not key:
        sys.exit("✗ 未配置 DEEPSEEK_API_KEY")
    resp = requests.post(
        API_URL,
        headers={"Authorization": "Bearer " + key, "Content-Type": "application/json"},
        json={
            "model": MODEL,
            "messages": [{"role": "system", "content": "你是专业的财经简报主笔，只输出符合要求的 JSON。"},
                         {"role": "user", "content": prompt}],
            "temperature": 0.6,
            "max_tokens": 16384,
            "stream": False,
        },
        timeout=300,
    )
    if resp.status_code != 200:
        sys.exit("✗ DeepSeek API HTTP %s: %s" % (resp.status_code, resp.text[:300]))
    data = resp.json()
    try:
        choice = data["choices"][0]
        return choice["message"]["content"], choice.get("finish_reason")
    except (KeyError, IndexError, TypeError):
        sys.exit("✗ DeepSeek 返回格式异常: %s" % str(data)[:300])


def extract_json(text: str) -> dict | None:
    """从模型输出提取 JSON 对象（容忍 ```json 包裹或前后杂质）。"""
    text = text.strip()
    fence = re.search(r"```(?:json)?\s*(.*?)```", text, re.S)
    if fence:
        text = fence.group(1).strip()
    start = text.find("{")
    if start < 0:
        return None
    # 从第一个 { 到最后一个 } 截取
    end = text.rfind("}")
    if end <= start:
        return None
    try:
        return json.loads(text[start:end + 1])
    except (ValueError, TypeError):
        return None


def _relevance_score(item: dict, text: str) -> float:
    """粗略主题相关度：text 中出现于该新闻标题/摘要的词（英文词或中文双字）越多分越高。"""
    target = (item["title"] + " " + item["summary"]).lower()
    words = re.findall(r"[a-z]{2,}", text.lower())
    chars = re.findall(r"[一-鿿]{2}", text)
    stop = {"过去", "核心", "变化", "以及", "还是", "今日", "昨天", "今天", "一些", "一个", "可以",
            "进行", "市场", "数据", "美元", "可能", "显示", "出现", "开始", "继续", "相关", "主要"}
    hits = sum(1 for w in words if w in target)
    hits += sum(1 for c in chars if c not in stop and c in target)
    return hits


def auto_sources(content: dict, pool: list[dict], section_ids: set[str]) -> None:
    """按各板块正文与新闻池的关键词相关度，为每个板块自动挂 1 条最相关真实来源。
    完全由脚本确定，不依赖模型生成 sources，保证时效与语义都可靠。"""
    sources_map = {}  # (source_name, title, published_at) -> set(section_ids)
    for sec in content.get("sections", []):
        if not isinstance(sec, dict):
            continue
        sid = sec.get("id")
        if sid not in section_ids:
            continue
        text = " ".join([
            sec.get("title", ""), sec.get("note", ""),
            *[str(b) for b in (sec.get("bullets") or [])],
        ])
        text = re.sub(r"[\*\[\]:,，。；;：（）()0-9.%/\-—–·]", " ", text)
        best, best_score = None, 0.0
        for item in pool:
            score = _relevance_score(item, text)
            if score > best_score:
                best, best_score = item, score
        if not best:
            best = pool[-1] if pool else None
        if best:
            key = (best["source_name"], best["title"], best["published_at"])
            sources_map.setdefault(key, set()).add(sid)
    content["sources"] = [
        {"source": k[0], "title": k[1], "published_at": k[2], "used_in": sorted(v)}
        for k, v in sources_map.items()
    ]


def sanitize_content(content: dict) -> None:
    """把模型输出里的 None 递归替换为空字符串，并保证关键结构存在，防止 build 崩溃。"""
    if isinstance(content, dict):
        for key, value in list(content.items()):
            if value is None:
                content[key] = ""
            elif isinstance(value, (dict, list)):
                sanitize_content(value)
    elif isinstance(content, list):
        for i, value in enumerate(content):
            if value is None:
                content[i] = ""
            elif isinstance(value, (dict, list)):
                sanitize_content(value)
    lc = content.get("logic_chain") if isinstance(content, dict) else None
    if isinstance(lc, dict):
        lc.setdefault("priority", "")
        for path in lc.get("paths") or []:
            if not isinstance(path, dict):
                continue
            for field in ("name", "color", "body", "sub"):
                path[field] = str(path.get(field) or "")
    if isinstance(content, dict):
        for field in ("alert", "main_title", "index_note", "qdii_date_label", "date_cn", "alert_time"):
            if field in content:
                content[field] = str(content.get(field) or "")
        sections = content.get("sections")
        if isinstance(sections, list):
            for sec in sections:
                if not isinstance(sec, dict):
                    continue
                for field in ("id", "title", "note"):
                    sec[field] = str(sec.get(field) or "")
                if not isinstance(sec.get("bullets"), list):
                    sec["bullets"] = []
                normalized_bullets = []
                for bullet in sec["bullets"]:
                    # DeepSeek occasionally emits {"text": ..., "emphasis": ...} even
                    # though the schema requires strings. Extract the actual copy; never
                    # stringify the object, which would leak Python/JSON syntax to readers.
                    if isinstance(bullet, dict) and isinstance(bullet.get("text"), str):
                        normalized_bullets.append(bullet["text"])
                    elif bullet is None:
                        normalized_bullets.append("")
                    else:
                        normalized_bullets.append(bullet)
                sec["bullets"] = normalized_bullets
        calendar = content.get("calendar")
        if isinstance(calendar, list):
            for entry in calendar:
                if isinstance(entry, dict):
                    for field in ("date", "event"):
                        entry[field] = str(entry.get(field) or "")


def ensure_note_floor(content: dict) -> int:
    """Pad only undersized notes with a section-specific verification sentence."""
    changed = 0
    for section in content.get("sections", []):
        if not isinstance(section, dict):
            continue
        note = section.get("note")
        if not isinstance(note, str) or len(note) >= NOTE_MIN:
            continue
        suffix = NOTE_FLOOR_SUFFIXES.get(section.get("id"), "后续继续观察关键数据与盘面信号能否相互验证。")
        separator = "" if not note or note.endswith(("。", "！", "？")) else "。"
        expanded = note + separator + suffix
        if len(expanded) < NOTE_MIN:
            expanded += "并持续跟踪后续变化。"
        section["note"] = expanded
        changed += 1
    return changed


def _pad_visible_to_floor(text: str, floor: int, suffix: str) -> str:
    """在文本末尾追加验证句，直到可见字数达到 floor（不引入新事实/数字）。"""
    guard = 0
    while visible_text_length(text) < floor and guard < 6:
        stripped = text.rstrip()
        separator = "" if not stripped or stripped.endswith(("。", "！", "？", "”")) else "。"
        text = stripped + separator + suffix
        guard += 1
    return text


def ensure_length_floors(content: dict, min_bullet_chars: dict | None = None) -> int:
    """把差几个字的 alert / bullet 碎段补到可见字数下限：段末追加一句板块专属验证条件，
    与 ensure_note_floor 同理，避免因 1-2 字缺口触发整篇无效重试。只补非空字段，
    空 bullet 仍由结构校验拦截（不凭空造整段）。幂等。"""
    min_bullet_chars = min_bullet_chars or MIN_BULLET_CHARS
    changed = 0
    alert = content.get("alert")
    if isinstance(alert, str) and 0 < visible_text_length(alert) < ALERT_MIN:
        content["alert"] = _pad_visible_to_floor(alert, ALERT_MIN, ALERT_FLOOR_SUFFIX)
        changed += 1
    for section in content.get("sections", []):
        if not isinstance(section, dict) or not isinstance(section.get("bullets"), list):
            continue
        floor = min_bullet_chars.get(section.get("id"))
        if not floor:
            continue
        suffix = BULLET_FLOOR_SUFFIXES.get(section.get("id"), DEFAULT_BULLET_SUFFIX)
        new_bullets = []
        for bullet in section["bullets"]:
            if isinstance(bullet, str) and 0 < visible_text_length(bullet) < floor:
                bullet = _pad_visible_to_floor(bullet, floor, suffix)
                changed += 1
            new_bullets.append(bullet)
        section["bullets"] = new_bullets
    return changed


def visible_text_length(text: str) -> int:
    """Count reader-visible copy, excluding lightweight formatting syntax."""
    if not isinstance(text, str):
        return 0
    text = re.sub(r"\[\[(?:red|green|c):(.+?)\]\]", r"\1", text)
    text = text.replace("**", "")
    text = re.sub(r"<br\s*/?>", "", text, flags=re.I)
    return len(text)


def _split_sentences(text: str) -> list[str]:
    """按中文句末标点切句并保留标点。bullet 为单行纯文本，无 <br>。"""
    return [t for t in re.split(r"(?<=[。！？])", text) if t]


def _sentence_has_fact(sentence: str) -> bool:
    """含数字或强调色的句子视为事实/关键结论，压缩时绝不删除。"""
    return bool(re.search(r"\d", sentence) or re.search(r"\[\[(?:red|green|c):", sentence))


def compress_bullets(content: dict, need_cut: int, min_bullet_chars: dict | None = None) -> int:
    """确定性压缩：删除板块 bullet 中“既无数字也无强调色”的冗余句，返回删除的可见字数。

    安全约束：只动 sections[].bullets（不碰 alert / logic_chain / note / 标题 / 日历 / 来源）；
    每条 bullet 保留首句与全部含数字或强调色的句子，只删中间/末尾的纯衔接或解读句，
    首句永不删，且至少保留两句、不低于板块字数下限。可见正文里的所有事实、数字、机构
    观点与多空方向因此原样保留，仅去掉重复背景与软性过渡。分两轮：先删中间句（保护结论
    末句），仍不足再允许删末句。幂等：无可删句或已在区间内时返回 0。"""
    if need_cut <= 0:
        return 0
    min_bullet_chars = min_bullet_chars or MIN_BULLET_CHARS
    removed = 0
    sections = [s for s in content.get("sections", [])
                if isinstance(s, dict) and isinstance(s.get("bullets"), list)]

    def sec_size(sec):
        return sum(visible_text_length(b) for b in sec["bullets"] if isinstance(b, str))

    for allow_last in (False, True):  # 优先删中间句，第二轮才允许删末句(结论)
        if removed >= need_cut:
            break
        for section in sorted(sections, key=sec_size, reverse=True):
            if removed >= need_cut:
                break
            floor = min_bullet_chars.get(section.get("id"), 0)
            order = sorted(
                range(len(section["bullets"])),
                key=lambda i: (visible_text_length(section["bullets"][i])
                               if isinstance(section["bullets"][i], str) else 0),
                reverse=True,
            )
            for i in order:
                if removed >= need_cut:
                    break
                bullet = section["bullets"][i]
                if not isinstance(bullet, str):
                    continue
                keep = _split_sentences(bullet)
                if len(keep) <= 2:
                    continue
                last_idx = len(keep) - 1
                for j in range(last_idx, 0, -1):  # 首句(0)永不删
                    if removed >= need_cut:
                        break
                    if j == last_idx and not allow_last:
                        continue
                    s = keep[j]
                    if s is None or _sentence_has_fact(s):
                        continue
                    alive = [k for k in keep if k]
                    if len(alive) <= 2:  # 至少保留两句
                        break
                    cand = visible_text_length(s)
                    if visible_text_length("".join(alive)) - cand < floor:
                        continue  # 不得低于板块字数下限
                    keep[j] = None
                    removed += cand
                new_bullet = "".join(k for k in keep if k)
                if new_bullet != bullet:
                    section["bullets"][i] = new_bullet
    return removed


def _calendar_keywords(event: str) -> list[str]:
    """从事件名提取候选关键词：前缀 + 剥掉"二季度财报/财报/报告/数据"等后缀的实体名。"""
    kws = set()
    for n in (6, 4):
        if len(event) >= n:
            kws.add(event[:n])
    for suf in ("二季度财报", "季度财报", "财报", "就业报告", "通胀报告", "利率决议", "报告", "数据"):
        if event.endswith(suf) and len(event) > len(suf):
            kws.add(event[:-len(suf)])
    return [k for k in kws if k]


def sanitize_calendar(content: dict, pool: list[dict], as_of: str) -> list[str]:
    """calendar 每条必须有新闻依据，防止模型凭训练记忆编造日期（2026-08-07 曾把英伟达财报写成 8/10，实为 8/26）。
    依据规则：事件关键词须出现在新闻池；日期要么等于本期 as_of（"今晚/今日"类相对日期），
    要么以"X月X日 / YYYY-MM-DD / X-X"形式出现在新闻池原文。无日期依据的条目删除。"""
    if not isinstance(content.get("calendar"), list):
        return []
    pool_text = "\n".join(
        (it.get("title", "") + " " + it.get("summary", "") if isinstance(it, dict) else "")
        for it in pool
    )
    today = str(as_of)[:10]
    kept, warnings = [], []
    for c in content["calendar"]:
        if not isinstance(c, dict):
            continue
        date = str(c.get("date", ""))[:10]
        event = str(c.get("event", ""))
        # 事件关键词（实体名 + 前缀，避免"美国"这类过泛词导致误保留）
        kw_hit = any(k in pool_text for k in _calendar_keywords(event))
        if date == today:
            # "今晚/今日"类相对日期：事件词或当日时点标记须在新闻池
            if kw_hit or any(m in pool_text for m in ("今晚", "今日", "今天", "20:30")):
                kept.append(c)
            else:
                warnings.append("calendar「%s」日期 %s（今日）在新闻池无当日依据，已删除" % (event, date))
            continue
        # 未来日期：必须事件关键词 + 明确日期形式 都出现在新闻池，否则删除（防编造）
        if not kw_hit:
            warnings.append("calendar 事件「%s」在新闻池无依据，已删除" % event)
            continue
        try:
            m = int(date[5:7]); d = int(date[8:10])
        except ValueError:
            warnings.append("calendar「%s」日期 %s 无效，已删除" % (event, date))
            continue
        forms = ["%d月%d日" % (m, d), date, "%02d-%02d" % (m, d), "%d/%d" % (m, d)]
        if not any(f in pool_text for f in forms):
            warnings.append("calendar「%s」日期 %s 在新闻池无日期依据（避免编造日期），已删除" % (event, date))
            continue
        kept.append(c)
    # 财经日历必须按日期升序（2026-08-07 用户指出改完英伟达日期后未重排，导致 08-26 排在 08-11 前）
    content["calendar"] = sorted(kept, key=lambda c: str(c.get("date", ""))[:10])
    return warnings


def post_process(content: dict, snap: dict, pool: list[dict], day: str, date_cn: str,
                 alert_time: str, as_of: str, index_note: str, qdii_label: str,
                 calendar_events: list[dict] | None = None,
                 min_bullet_chars: dict | None = None) -> list[str]:
    """统一修正日期标识、来源时效，禁止手写 indices/commodities。返回警告。"""
    warnings = []
    sanitize_content(content)
    # 强制三路径颜色为旧版高质量样式（科技紫/逆风红/独立变量绿）。
    lc = content.get("logic_chain") if isinstance(content, dict) else None
    if isinstance(lc, dict):
        for i, path in enumerate(lc.get("paths") or []):
            if isinstance(path, dict) and i < len(PATH_COLORS):
                path["color"] = PATH_COLORS[i]
        priority = lc.get("priority")
        if isinstance(priority, list) and all(isinstance(item, str) for item in priority):
            lc["priority"] = "；".join(item.strip() for item in priority if item.strip())
            warnings.append("logic_chain.priority 列表已归一化为字符串")
    content["date_yyyymmdd"] = day
    content["date_cn"] = date_cn
    content["as_of"] = as_of
    content["alert_time"] = alert_time
    content["index_note"] = index_note
    content["qdii_date_label"] = qdii_label
    content["main_title"] = normalize_main_title(content.get("main_title"))
    content.pop("indices", None)
    content.pop("commodities", None)
    # 过滤掉模型输出的非对象/无效 sections，防止后续崩溃
    sections = content.get("sections")
    if isinstance(sections, list):
        content["sections"] = [sec for sec in sections if isinstance(sec, dict) and sec.get("id")]
    else:
        content["sections"] = []
    section_ids = {sec.get("id") for sec in content.get("sections", []) if sec.get("id")}
    for sec in content.get("sections", []):
        sid = sec.get("id")
        minimum = BULLET_RULES.get(sid, (1, 99))[0]
        if not isinstance(sec.get("bullets"), list) or len(sec["bullets"]) < minimum:
            warnings.append("板块 %s bullets 少于%d条" % (sid, minimum))
    emphasis_trimmed = trim_long_emphasis(content)
    if emphasis_trimmed:
        warnings.append("已将 %d 处超长强调色收窄到 40 字内（不改可见正文）" % emphasis_trimmed)
    floors_filled = ensure_length_floors(content, min_bullet_chars)
    if floors_filled:
        warnings.append("已为 %d 个差几字的 alert/bullet 碎段补足板块专属验证条件" % floors_filled)
    notes_filled = ensure_note_floor(content)
    if notes_filled:
        warnings.append("已为 %d 个过短 note 补充板块专属验证条件" % notes_filled)
    emphasis_added = ensure_emphasis(content)
    if emphasis_added:
        warnings.append("已在不改正文文字的前提下确定性补齐 %d 处强调色" % emphasis_added)
    auto_sources(content, pool, section_ids)
    if calendar_events:
        warnings += apply_calendar_feed(content, calendar_events)
    else:
        # 兜底：日历源不可用时退回旧的新闻池溯源清洗，宁可少、不可错。
        warnings += sanitize_calendar(content, pool, as_of)
    return warnings


def title_ok(main_title) -> bool:
    """标题最多两行，每行不超过 24 字；允许短而有力的双线标题。"""
    if not isinstance(main_title, str):
        return False
    lines = [re.sub(r"\s+", "", line) for line in main_title.split("<br>") if line.strip()]
    if len(lines) == 1:
        return len(lines[0]) <= TITLE_LINE_MAX
    if len(lines) == 2:
        return all(len(line) <= TITLE_LINE_MAX for line in lines)
    return False


def normalize_main_title(main_title) -> str:
    """修复模型常见的纯格式错误，避免为了换行规则浪费一次完整写稿。

    - 一到两行且每行 <=24 字：保留原结构，双线标题不再被强制合并。
    - 合计 <=48 字但换行不合规：均衡分成两行。
    - 超过 48 字不截断语义，仍交给模型重写。
    """
    if not isinstance(main_title, str):
        return ""
    lines = [re.sub(r"\s+", "", line) for line in re.split(r"<br\s*/?>", main_title)
             if line.strip()]
    joined = "".join(lines)
    if 1 <= len(lines) <= 2 and all(len(line) <= TITLE_LINE_MAX for line in lines):
        return "<br>".join(lines)
    if len(joined) <= TITLE_LINE_MAX * 2:
        split_at = (len(joined) + 1) // 2
        return joined[:split_at] + "<br>" + joined[split_at:]
    return "<br>".join(lines)


def title_news_errors(main_title) -> list[str]:
    """标题内容的最小硬校验。

    真实性和重要性仍由新闻池与模型上下文约束；这里只拦截可确定识别的
    观点词、悬念标点、来源转述和完全没有事实动词的摘要式标题。
    """
    if not isinstance(main_title, str) or not main_title.strip():
        return ["标题为空"]
    plain = re.sub(r"<br\s*/?>|\s+", "", main_title)
    errors = []
    found = [phrase for phrase in TITLE_FORBIDDEN_PHRASES if phrase in plain]
    if found:
        errors.append("含观点型/模糊词：" + "、".join(found))
    if re.search(r"[!！?？]", plain):
        errors.append("不得使用问句或感叹号")
    if re.search(r"(?:据|有)(?:.{0,8})(?:报道|称|消息)", plain):
        errors.append("不得在标题中转述新闻来源")
    if not any(verb in plain for verb in TITLE_FACT_VERBS):
        errors.append("缺少已发生、可验证的事实动词")
    return errors


def fallback_main_title(snap: dict) -> str:
    """根据已校验行情生成必定合规的保底标题。

    仅在模型的标题单独修复也失败时使用，确保一个标题问题不会让整篇正文作废。
    """
    aliases = {
        "标普500": "标普500",
        "纳斯达克综合": "纳斯达克",
        "道琼斯": "道指",
        "费城半导体": "费半",
    }
    ranked = []
    for row in snap.get("indices", []):
        name = row.get("name")
        if name not in aliases:
            continue
        match = re.search(r"[-+]?\d+(?:\.\d+)?", str(row.get("chg", "")))
        if not match:
            continue
        change = float(match.group())
        ranked.append((abs(change), change, aliases[name]))
    if not ranked:
        return "美股指数持稳"
    _, change, name = max(ranked)
    verb = "上涨" if change > 0 else "下跌" if change < 0 else "持稳"
    title = name + verb
    # aliases 和动词均受控；保留断言防止后续改名破坏标题硬约束。
    assert title_ok(title) and not title_news_errors(title)
    return title


def ensure_main_title(content: dict, snap: dict, pool: list[dict]) -> str | None:
    """确保标题合规；正文已经有效时只修标题，不重新生成整篇。

    返回一条供日志展示的修复说明；原标题已合规则返回 None。
    """
    original = normalize_main_title(content.get("main_title"))
    content["main_title"] = original
    original_errors = []
    if not title_ok(original):
        original_errors.append("格式违规")
    original_errors.extend(title_news_errors(original))
    if not original_errors:
        return None

    news_lines = []
    for item in pool[:20]:
        text = item["title"] + ("。" + item["summary"] if item["summary"] else "")
        news_lines.append("- " + text[:240])
    focus = next((sec for sec in content.get("sections", [])
                  if isinstance(sec, dict) and sec.get("id") == "focus"), {})
    prompt = (
        "请只修复下面财经简报的 main_title。只输出一个 JSON 对象，格式为"
        "{\"main_title\":\"标题\"}，不要输出其他文字。\n"
        "硬规则：标题必须由具体主体、已发生动作和关键数字构成；事实只能来自候选新闻；"
        "优先写成两行，第一行是主事件+市场反应，第二行是另一条影响定价的副线；"
        "禁止观点、预测、问句、感叹号和来源转述；最多两行、每行最多24字符。"
        "必须含已发生、可验证的事实动词，例如发布、宣布、增长、翻倍、上涨、下跌、"
        "大涨、回落、推出、签署、恢复。\n"
        "原标题：%s\n头号焦点：%s\n摘要：%s\n候选新闻：\n%s"
        % (original, focus.get("title", ""), str(content.get("alert", ""))[:500],
           "\n".join(news_lines))
    )
    try:
        raw, _ = call_deepseek(prompt)
        repaired_obj = extract_json(raw)
        repaired = normalize_main_title(
            repaired_obj.get("main_title") if isinstance(repaired_obj, dict) else ""
        )
        if title_ok(repaired) and not title_news_errors(repaired):
            content["main_title"] = repaired
            return "main_title 已单独修复：%s → %s" % (original, repaired)
    except (Exception, SystemExit) as exc:  # 标题修复失败不得拖垮整篇正文
        print("  ⚠️ 标题单独修复调用失败：%s" % exc, file=sys.stderr)

    fallback = fallback_main_title(snap)
    content["main_title"] = fallback
    return "main_title 单独修复未通过，已使用行情兜底：%s → %s" % (original, fallback)


def validate_required(content: dict, day: str, bullet_rules: dict | None = None,
                      min_bullet_chars: dict | None = None) -> list[str]:
    bullet_rules = bullet_rules or BULLET_RULES
    min_bullet_chars = min_bullet_chars or MIN_BULLET_CHARS
    errors = []
    errors.extend(validate_no_structure_leaks(content))
    for field in ("date_yyyymmdd", "date_cn", "as_of", "alert_time", "alert",
                  "index_note", "qdii_date_label", "logic_chain", "calendar", "sources", "sections"):
        if field not in content:
            errors.append("缺字段 " + field)
    if content.get("date_yyyymmdd") != day:
        errors.append("date_yyyymmdd 应为 %s" % day)
    if not isinstance(content.get("sections"), list) or not content.get("sections"):
        errors.append("sections 为空")
    if not isinstance(content.get("sources"), list) or not content.get("sources"):
        errors.append("sources 为空")
    alert = content.get("alert")
    if not isinstance(alert, str):
        errors.append("alert 应为字符串")
    elif not ALERT_MIN <= visible_text_length(alert) <= ALERT_MAX:
        errors.append("alert 可见正文应为 %d-%d 字，实际 %d 字" % (
            ALERT_MIN, ALERT_MAX, visible_text_length(alert)))
    sections = content.get("sections")
    if isinstance(sections, list):
        ids = [sec.get("id") for sec in sections if isinstance(sec, dict)]
        expected = ["focus", "ai_tech", "stock_rotation"]
        if "iran" in ids:
            expected.append("iran")
        expected.extend(["macro", "commodity"])
        if ids != expected:
            errors.append("sections 顺序/集合应为 %s，实际为 %s" % (expected, ids))
        for index, sec in enumerate(sections):
            if not isinstance(sec, dict):
                errors.append("sections[%d] 不是对象" % index)
                continue
            sid = sec.get("id")
            for field in ("id", "title", "bullets", "note"):
                if sec.get(field) in (None, "", []):
                    errors.append("板块 %s 缺少/空字段 %s" % (sid or index, field))
            bullets = sec.get("bullets")
            if sid in bullet_rules and isinstance(bullets, list):
                low, high = bullet_rules[sid]
                if not low <= len(bullets) <= high:
                    expected_count = str(low) if low == high else "%d-%d" % (low, high)
                    errors.append("板块 %s bullets 应为 %s 条，实际 %d 条" % (
                        sid, expected_count, len(bullets)))
                for bullet_index, bullet in enumerate(bullets, 1):
                    if not isinstance(bullet, str) or not bullet.strip():
                        errors.append("板块 %s bullet[%d] 为空" % (sid, bullet_index))
                    elif (sid in min_bullet_chars
                          and visible_text_length(bullet) < min_bullet_chars[sid]):
                        errors.append("板块 %s bullet[%d] 可见正文至少 %d 字，实际 %d 字" % (
                            sid, bullet_index, min_bullet_chars[sid],
                            visible_text_length(bullet)))
            note = sec.get("note")
            if not isinstance(note, str):
                errors.append("板块 %s note 应为字符串" % sid)
            elif not NOTE_MIN <= len(note) <= NOTE_MAX:
                errors.append("板块 %s note 应为 %d-%d 字，实际 %d 字" % (
                    sid, NOTE_MIN, NOTE_MAX, len(note)))
    logic_chain = content.get("logic_chain")
    if not isinstance(logic_chain, dict):
        errors.append("logic_chain 不是对象")
    else:
        paths = logic_chain.get("paths")
        if not isinstance(paths, list) or len(paths) != 3:
            errors.append("logic_chain.paths 应固定为 3 项")
        else:
            for index, path in enumerate(paths, 1):
                if not isinstance(path, dict):
                    errors.append("logic_chain.paths[%d] 不是对象" % index)
                    continue
                missing = [field for field in ("name", "color", "body", "sub")
                           if not isinstance(path.get(field), str) or not path[field].strip()]
                if missing:
                    errors.append("logic_chain.paths[%d] 缺少/非字符串字段 %s" % (
                        index, ",".join(missing)))
        if not isinstance(logic_chain.get("priority"), str) or not logic_chain["priority"].strip():
            errors.append("logic_chain.priority 应为非空字符串")
    if not isinstance(content.get("calendar"), list):
        errors.append("calendar 应为数组")
    errors.extend(validate_calendar(content))
    errors.extend(validate_emphasis(content))
    return errors


def write_content(path: Path, content: dict) -> None:
    """Always serialize generated content through json.dump()."""
    with path.open("w", encoding="utf-8") as handle:
        json.dump(content, handle, ensure_ascii=False, indent=2)
        handle.write("\n")


def run_build(day: str) -> tuple[int, str]:
    proc = subprocess.run(
        [str(BASE / ".venv/bin/python"), str(BASE / "build_brief.py"), str(BASE / ("content_%s.json" % day))],
        capture_output=True, text=True,
    )
    return proc.returncode, (proc.stdout + proc.stderr)


def parse_count(out: str) -> int | None:
    m = re.search(r"正文字符数\(去标签\):\s*(\d+)", out)
    return int(m.group(1)) if m else None


def parse_section_counts(out: str) -> str:
    m = re.search(r"分段字数:\s*(.+)", out)
    return m.group(1).strip() if m else ""


def main() -> int:
    args = sys.argv[1:]
    views_text = ""
    if "--views" in args:
        idx = args.index("--views")
        if idx + 1 < len(args):
            views_path = Path(args[idx + 1])
            if views_path.exists():
                views_text = views_path.read_text(encoding="utf-8", errors="ignore")[:36000]
            args = args[:idx] + args[idx + 2:]
    snap = load_snapshot()
    as_of = snap["run_started_at"]
    as_of_dt = parse_iso(as_of)
    if not as_of_dt:
        sys.exit("✗ market_snapshot.run_started_at 无效")
    day = args[0] if args else as_of_dt.strftime("%Y%m%d")
    date_cn = "%d年%d月%d日（%s）" % (as_of_dt.year, as_of_dt.month, as_of_dt.day,
                                     WEEKDAYS[as_of_dt.weekday()])
    alert_time = as_of_dt.strftime("%H:%M")
    price_session_date = snap.get("price_index_session_date") or snap["indices"][0]["session_date"]
    price_sd = dt.date.fromisoformat(price_session_date)
    idx_day = "%d月%d日" % (price_sd.month, price_sd.day)
    index_note = ("各标的均采用本次截止点前最新可得报价；美股指数为%s收盘，"
                  "黄金、布伦特与美元为本期截止前盘中报价，10年美债收益率为最近交易值。" % idx_day)
    qdii_label = "估算基于%d年%d月%d日（%s）最近交易日收盘" % (
        price_sd.year, price_sd.month, price_sd.day, WEEKDAYS[price_sd.weekday()]
    )

    # 淡日感知：美股休市/长假后首日隔夜无新成交、过夜新闻偏薄，放宽字数下限与 ai_tech 条数下限。
    profile = THIN_PROFILE if is_thin_session(snap, as_of_dt.date()) else NORMAL_PROFILE
    if profile["thin"]:
        print("淡日档位：美股最近交易日为 %s（长假/休市后首日），字数下限放宽至 %d，目标 %d-%d"
              % (price_session_date, profile["hard_min"], profile["target_min"], profile["target_max"]))

    window_start = parse_iso(snap["news_window"]["start"])
    window_end = parse_iso(snap["news_window"]["end"])
    pool = build_news_pool(window_start, window_end)
    print("新闻池：%d 条（窗口 %s 至 %s）" % (len(pool), snap["news_window"]["start"], snap["news_window"]["end"]))

    calendar_events, calendar_warnings = load_calendar_feed()
    for warning in calendar_warnings:
        print("⚠️ 财经日历：%s" % warning, file=sys.stderr)
    if calendar_events:
        print("本周财经日历：%d 条（确定性来源）" % len(calendar_events))
    elif day >= CALENDAR_REQUIRED_FROM:
        sys.exit("✗ 本周财经日历不可用，禁止写稿 —— 先运行 fetch_calendar.py")

    # 飞书观点先提炼为纯"方向立场清单"，防止其标题/措辞/素材被当作简报内容引用
    views_for_prompt = ""
    if views_text.strip():
        print("提炼飞书观点为立场清单 ...", file=sys.stderr)
        try:
            views_for_prompt = extract_stance(views_text)
            print("立场清单（%d 字）" % len(views_for_prompt), file=sys.stderr)
        except Exception as exc:  # noqa: BLE001 - 提炼失败不阻断
            print("⚠️ 立场提炼失败(%s)，本次不参考观点" % exc, file=sys.stderr)
            views_for_prompt = ""

    feedback = None
    final_count = None
    final_build_rc = None
    parsed_any = False
    wrote_candidate = False
    candidate = None
    best_degraded = None  # (count, content)：结构全过、仅整稿略短于硬下限的最佳候选
    best_overshoot = None  # (count, content)：结构全过、build rc=0，仅整稿略高于硬上限的最佳候选
    for attempt in range(1, MAX_ATTEMPTS + 1):
        action = "生成初稿" if candidate is None else "定向修复"
        print("写稿尝试 %d/%d（%s）..." % (attempt, MAX_ATTEMPTS, action), file=sys.stderr)
        if candidate is None:
            prompt = build_prompt(snap, pool, day, date_cn, alert_time, as_of, index_note,
                                  qdii_label, views_for_prompt, feedback, calendar_events, profile)
        else:
            prompt = build_repair_prompt(
                candidate, feedback or "请修复并通过全部校验。", pool,
                day, date_cn, alert_time, as_of, index_note, qdii_label, profile,
            )
        raw, finish_reason = call_deepseek(prompt)
        Path("/tmp/wb_last_raw.txt").write_text(raw, encoding="utf-8")
        print("  原始输出 %d 字符, finish=%s" % (len(raw), finish_reason), file=sys.stderr)
        if finish_reason == "length":
            feedback = "上次输出因过长被截断，请精简每条的措辞但保持全部板块与字段完整，确保输出完整 JSON。"
            print("  输出被截断，重试", file=sys.stderr)
            continue
        content = extract_json(raw)
        if content is None:
            feedback = "上次输出不是有效 JSON，请只输出一个 JSON 对象，不要任何其他文字。"
            print("  解析失败，重试", file=sys.stderr)
            continue
        parsed_any = True
        warnings = post_process(content, snap, pool, day, date_cn, alert_time, as_of, index_note,
                                qdii_label, calendar_events, profile["min_bullet_chars"])
        candidate = content
        errors = validate_required(content, day, profile["bullet_rules"], profile["min_bullet_chars"])
        if errors:
            feedback = "上次输出不完整：" + "；".join(errors)
            print("  结构校验失败：" + "；".join(errors), file=sys.stderr)
            continue
        title_warning = ensure_main_title(content, snap, pool)
        if title_warning:
            warnings.append(title_warning)
            print("  ⚠️ " + title_warning, file=sys.stderr)
        out_path = BASE / ("content_%s.json" % day)
        write_content(out_path, content)
        wrote_candidate = True
        rc, out = run_build(day)
        final_build_rc = rc
        count = parse_count(out)
        if rc != 0:
            tail = "\n".join(out.strip().splitlines()[-6:])
            feedback = "构建校验失败，请只修正相关字段。错误：\n" + tail
            print("  构建校验失败", file=sys.stderr)
            print("  " + tail.replace("\n", "\n  "), file=sys.stderr)
            continue
        final_count = count
        if count is None:
            feedback = "无法解析字数，请确保结构完整。"
            print("  字数解析失败", file=sys.stderr)
            continue
        hmin, hmax = profile["hard_min"], profile["hard_max"]
        tmin, tmax = profile["target_min"], profile["target_max"]
        print("  正文 %d 字（硬约束 %d-%d，目标 %d-%d%s）"
              % (count, hmin, hmax, tmin, tmax, "，淡日档" if profile["thin"] else ""))
        if hmin <= count <= hmax:
            for w in warnings:
                print("  ⚠️ " + w, file=sys.stderr)
            print("✓ 写稿完成：%s" % out_path)
            return 0
        # 结构/时效/日历/强调色全过、build rc=0，仅整稿略短于硬下限——记为降级放行候选（取最长者）
        if profile["accept_min"] <= count < hmin and (best_degraded is None or count > best_degraded[0]):
            best_degraded = (count, content)
        # 仅整稿略高于硬上限——记为确定性压缩候选（取最接近上限者，尽量少删）
        if hmax < count <= hmax + OVERSHOOT_TOLERANCE and (best_overshoot is None or count < best_overshoot[0]):
            best_overshoot = (count, content)
        delta = tmax - count
        direction = "增加" if delta > 0 else "减少"
        section_counts = parse_section_counts(out)
        if delta > 0:
            adjustment = (
                "扩充：优先整体扩写 focus/ai_tech 的现有 bullet，补足数据背景、"
                "产业链传导、交叉验证与风险情景；不得编造新事实或新数字。"
            )
        else:
            adjustment = (
                "精简：优先按分段字数压缩最大的 2-3 个板块，删掉重复背景和重复解读，"
                "保留新闻事实、关键数字、逻辑链与结论。"
            )
        feedback = ("上次正文 %d 字，需%s约 %d 字（向目标 %d-%d 靠拢）。"
                    "分段字数：%s。%s不要逐字微调，保留合格板块，"
                    "定向修订后输出完整 JSON。"
                    % (count, direction, abs(delta), tmin, tmax,
                       section_counts, adjustment))
        print("  字数 %s，重试" % ("偏少" if delta > 0 else "超限"), file=sys.stderr)

    # 超限侧确定性压缩兜底：模型在硬上限附近反复不收敛时，删除冗余衔接/解读句压回区间。
    # 压缩后落入 [hard_min, hard_max] 即完全合规（区别于偏短侧的降级放行），返回 0。
    if best_overshoot is not None:
        count, content = best_overshoot
        hmin, hmax = profile["hard_min"], profile["hard_max"]
        removed = compress_bullets(content, count - (hmax - COMPRESS_MARGIN),
                                   profile["min_bullet_chars"])
        if removed > 0:
            write_content(BASE / ("content_%s.json" % day), content)
            rc, out = run_build(day)
            new_count = parse_count(out)
            if rc == 0 and new_count is not None and hmin <= new_count <= hmax:
                print("⚠️ 确定性压缩：正文由 %d 字精简至 %d 字（删除 %d 可见字的冗余衔接/解读句，"
                      "保留全部数字、强调色与观点方向），已写入正式 content_%s.json 继续构建发布。"
                      % (count, new_count, removed, day), file=sys.stderr)
                print("✓ 写稿完成（超限侧确定性压缩）：content_%s.json" % day)
                return 0
            print("⚠️ 确定性压缩后仍未落入硬区间（%s 字，build rc=%s），转人工复核。"
                  % (new_count, rc), file=sys.stderr)

    if best_degraded is not None:
        count, content = best_degraded
        write_content(BASE / ("content_%s.json" % day), content)
        print("⚠️ 降级放行：正文 %d 字（低于硬下限 %d，但 ≥ 容差下限 %d，且结构/时效/日历/强调色全过、"
              "build rc=0），已保留为正式 content_%s.json 继续构建发布。"
              % (count, profile["hard_min"], profile["accept_min"], day), file=sys.stderr)
        print("✓ 写稿完成（降级放行，字数偏少）：content_%s.json" % day)
        return 3

    if wrote_candidate:
        print("✗ 连续 %d 次写稿未达硬约束（最后正文 %s 字，构建 rc=%s）；"
              "已保留最后一次 content_%s.json，请人工复核或手动调整。"
              % (MAX_ATTEMPTS, final_count, final_build_rc, day), file=sys.stderr)
    elif parsed_any:
        print("✗ 连续 %d 次均返回可解析 JSON，但结构校验未通过；"
              "未写入正式 content_%s.json。" % (MAX_ATTEMPTS, day), file=sys.stderr)
    else:
        print("✗ 连续 %d 次均未获得可解析 JSON；未写入 content_%s.json。"
              % (MAX_ATTEMPTS, day), file=sys.stderr)
    # 2=模型已返回 JSON，但内容校验未通过；1=API/响应解析层失败。
    return 2 if parsed_any else 1


if __name__ == "__main__":
    raise SystemExit(main())
