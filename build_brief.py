#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
股市马斯克 · 每日财经简报 渲染器
====================================
把「纯文本内容(JSON)」渲染成符合 AGENTS.md 规范的完整微信 HTML。
样式、表格脚手架、QDII 20 行表、页脚、字数统计全部由本脚本负责，
撰写时只需产出内容，不再手敲任何 <p style> / <table> / badge。

用法：
    python3 build_brief.py content_20260614.json [output.html]
    # 不给 output 时自动写 wechat_article_<date_yyyymmdd>.html

行情总览表(自动填充)：
    content json 不手写 indices / commodities。脚本只读取 fetch_market.py
    在本次实际执行时生成的项目内 market_snapshot.json。快照超过2小时、
    与 content.as_of 不匹配、核心标的不完整或任一报价缺时间/市场状态时直接报错。

QDII 数据来源优先级(自动降级)：
    1) 云端常驻接口 https://fundgz.ndxspx.cn/api/qdii-snapshot —— 首选,成功时落持久副本
    2) /tmp/qdii_cache.json —— 本机外部工具写入的易失缓存
    3) 项目内持久副本 qdii_cache.json —— 上次云端成功的快照(兜断网)
    4) content.json 里的 "qdii" 数组(20 项,顺序见 QDII_FUNDS) —— 最后兜底
       每项: {"pct":"+2.59%","status":"限大额","maxsg":100}
    注: est_chg 为 null(接口暂无估算)时,该基金渲染中性占位「—」,不显示 0/None。

正文轻量标记(在 bullets / note / alert 等文本里可用)：
    **粗体**          → <strong>
    [[red:文字]]      → 红色加粗(涨/利多 #A32D2D)
    [[green:文字]]    → 绿色加粗(跌/利空 #3B6D11)
    [[c:文字]]        → 本节强调色加粗(随 section 颜色)
"""
import sys, os, re, json, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from content_validation import emphasis_summary, validate_emphasis, validate_no_structure_leaks

# ---------- 颜色常量(A股惯例:涨红跌绿) ----------
RED   = "#A32D2D"   # 涨/利多
GREEN = "#3B6D11"   # 跌/利空
GOLD  = "#BA7517"   # 黄金板块
TECH  = "#534AB7"   # 半导体/科技
MACRO = "#0F6E56"   # 宏观/货币

# 从该期起，所有新闻事实必须登记来源发布时间，并通过严格 24 小时窗口校验。
# 旧内容样本没有来源元数据，因此不追溯强制。
FRESHNESS_REQUIRED_FROM = "20260727"
DYNAMIC_AS_OF_REQUIRED_FROM = "20260804"
BRIEF_TZ = ZoneInfo("Asia/Shanghai")
INTERNAL_MECHANISM_TERMS = (
    "项目行情快照",
    "行情快照显示",
    "飞书最新文章",
    "飞书文章",
    "飞书文档",
    "market_snapshot",
    "fetch_market",
    "fetch_views",
    "后台运行机制",
    "脚本运行",
    "脚本显示",
)
WEEKDAYS = ("周一", "周二", "周三", "周四", "周五", "周六", "周日")
FINAL_PLACEHOLDER_PATTERNS = (
    (r"COPY_FROM", "COPY_FROM 模板占位符"),
    (r"YYYYMMDD", "YYYYMMDD 模板占位符"),
    (r"\d{4}年X月|X月X日|\d{1,2}月X日", "X月/X日 模板占位符"),
    (r"周X", "周X 模板占位符"),
    (r"[（(]周[）)]", "星期内容为空"),
)

# 各板块默认 emoji / 强调色 / 彩框配色
SECTION_DEFAULTS = {
    "focus":          {"emoji": "🔥",  "color": RED,   "note": "red"},
    "ai_tech":        {"emoji": "🤖",  "color": TECH,  "note": "purple"},
    "stock_rotation": {"emoji": "📊",  "color": TECH,  "note": "purple"},
    "iran":           {"emoji": "🌐",  "color": RED,   "note": "red"},
    "macro":          {"emoji": "🏛️", "color": MACRO, "note": "green"},
    "commodity":      {"emoji": "🥇",  "color": GOLD,  "note": "gold"},
    # 旧 id 兼容(历史文件)
    "stock":          {"emoji": "💻",  "color": TECH,  "note": "purple"},
}
# 彩框(Note Bar)配色: bg / border / text  —— 微信兼容: <section>+background-color
NOTE_COLORS = {
    "red":    ("#FCEBEB", RED,   RED),
    "gold":   ("#fff3e0", GOLD,  GOLD),
    "purple": ("#EEEDF9", TECH,  TECH),
    "green":  ("#E8F5F0", MACRO, MACRO),
    "gray":   ("#f5f4f0", "#1a1a18", "#1a1a18"),
}

# 行情表固定标的名(顺序固定,不得更改)
INDEX_NAMES = ["标普500", "纳斯达克综合", "道琼斯", "费城半导体", "恐慌指数"]
# 大宗/外汇固定标的名 + 各行固定底色(沿用模板)
COMMODITY_ROWS = [
    ("黄金/美元",         "#fffdf5"),
    ("布伦特原油期货",   "#fff8f5"),
    ("10年美债收益率",   "#fff"),
    ("美元指数",         "#fafafa"),
]

# QDII 20 只固定基金(代码, 名称) —— 顺序固定
QDII_FUNDS = [
    ("002891", "华夏移动互联混合人民币"),
    ("018147", "建信新兴市场混合(QDII)C"),
    ("016665", "天弘全球高端制造混合(QDII)C"),
    ("021662", "国富亚洲机会股票(QDII)C"),
    ("021842", "国富全球科技互联混合(QDII)C"),
    ("012922", "易方达全球成长精选混合(QDII)C"),
    ("024239", "华夏全球科技先锋混合(QDII)C"),
    ("017731", "嘉实全球产业升级股票(QDII)C"),
    ("008254", "华宝致远混合(QDII)C"),
    ("014002", "浦银安盛全球智能科技(QDII)C"),
    ("018036", "长城全球新能源车股票(QDII)C"),
    ("022184", "富国全球科技互联网股票(QDII)C"),
    ("021277", "广发全球精选股票(QDII)C"),
    ("017654", "创金合信全球芯片产业股票(QDII)C"),
    ("016702", "银华海外数字经济量化选股(QDII)C"),
    ("017204", "华宝海外科技股票(QDII-LOF)C"),
    ("013328", "嘉实全球价值股票(QDII)"),
    ("000043", "嘉实美国成长股票人民币"),
    ("017437", "华宝纳斯达克精选股票(QDII)C"),
    ("017145", "华宝海外新能源汽车股票(QDII)C"),
]
QDII_API = "https://fundgz.ndxspx.cn/api/qdii-snapshot"              # 云端常驻接口(首选;2026-07-12 起走 HTTPS 域名,旧 IP:8088 防火墙已封)
QDII_CACHE = "/tmp/qdii_cache.json"                                   # 本机外部工具写入的易失缓存(/tmp 会被系统清空)
PROJECT_CACHE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "qdii_cache.json")  # 项目内持久副本(断网兜底)
PUBLIC_DATA_SOURCES = "天天基金 · 新浪财经 · 东方财富 · CBOE"


# ---------- 工具函数 ----------
def md(text, accent=RED):
    """解析正文轻量标记 → HTML 内联样式。accent 为 [[c:]] 用到的本节强调色。"""
    if text is None:
        return ""
    t = text
    t = re.sub(r"\[\[red:(.+?)\]\]",   r'<span style="color:%s;font-weight:700;">\1</span>' % RED,   t)
    t = re.sub(r"\[\[green:(.+?)\]\]", r'<span style="color:%s;font-weight:700;">\1</span>' % GREEN, t)
    t = re.sub(r"\[\[c:(.+?)\]\]",     r'<span style="color:%s;font-weight:700;">\1</span>' % accent, t)
    t = re.sub(r"\*\*(.+?)\*\*",       r"<strong>\1</strong>", t)
    return t


def pct_color(pct):
    """根据百分比字符串符号返回颜色: 负→绿, 正/有数→红, 无估值(—)→中性灰。"""
    s = str(pct).strip()
    if s.startswith("-"):
        return GREEN
    if s.startswith("+") or s[:1].isdigit():
        return RED
    return "#999"


def dir_color(d):
    return GREEN if d == "down" else RED


def _parse_source_time(value):
    """解析带时区的 ISO 8601 时间；不允许用无时区时间猜测发布时间。"""
    if not isinstance(value, str) or not value.strip():
        raise ValueError("published_at 为空")
    normalized = value.strip()
    if normalized.endswith("Z"):
        normalized = normalized[:-1] + "+00:00"
    dt = datetime.datetime.fromisoformat(normalized)
    if dt.tzinfo is None or dt.utcoffset() is None:
        raise ValueError("published_at 必须带时区偏移，例如 +08:00 或 Z")
    return dt.astimezone(BRIEF_TZ)


def validate_source_freshness(content):
    """校验新闻来源是否落在简报截止时点之前的严格 24 小时窗口内。

    content["sources"] 每项至少需要：
      source, title, published_at, used_in
    used_in 为板块 id 数组；每个正文板块至少要被一个有效来源覆盖。
    """
    date_key = str(content.get("date_yyyymmdd", ""))
    if date_key < FRESHNESS_REQUIRED_FROM:
        return "旧样本：未强制来源时效"

    try:
        brief_date = datetime.datetime.strptime(date_key, "%Y%m%d").date()
    except (TypeError, ValueError):
        sys.exit("✗ 时效校验失败：date_yyyymmdd 格式不正确")

    if content.get("as_of"):
        try:
            cutoff = _parse_source_time(content["as_of"])
        except (TypeError, ValueError) as exc:
            sys.exit("✗ 时效校验失败：as_of 无效：%s" % exc)
        if cutoff.date() != brief_date:
            sys.exit("✗ 时效校验失败：as_of 北京日期与 date_yyyymmdd 不一致")
    elif date_key >= DYNAMIC_AS_OF_REQUIRED_FROM:
        sys.exit(
            "✗ 时效校验失败：content 缺少 as_of；"
            "必须复制 market_snapshot.json.run_started_at，不得用08:20代替"
        )
    else:
        try:
            cutoff_time = datetime.datetime.strptime(content["alert_time"], "%H:%M").time()
        except (KeyError, TypeError, ValueError):
            sys.exit("✗ 时效校验失败：旧样本 alert_time 格式不正确")
        cutoff = datetime.datetime.combine(brief_date, cutoff_time, tzinfo=BRIEF_TZ)
    window_start = cutoff - datetime.timedelta(hours=24)
    sources = content.get("sources")
    if not isinstance(sources, list) or not sources:
        sys.exit(
            "✗ 时效校验失败：content 缺少 sources；"
            "每条新闻来源须登记 source/title/published_at/used_in"
        )

    section_ids = {
        section.get("id") for section in content.get("sections", [])
        if section.get("id")
    }
    covered_sections = set()
    errors = []

    for index, item in enumerate(sources, 1):
        if not isinstance(item, dict):
            errors.append("sources[%d] 不是对象" % index)
            continue
        label = item.get("title") or "未命名来源"
        if not item.get("source"):
            errors.append("sources[%d]《%s》缺少 source" % (index, label))
        if not item.get("title"):
            errors.append("sources[%d] 缺少 title" % index)
        try:
            published = _parse_source_time(item.get("published_at"))
            if published < window_start:
                errors.append(
                    "sources[%d]《%s》已超过24小时：%s（窗口起点 %s）"
                    % (
                        index,
                        label,
                        published.strftime("%Y-%m-%d %H:%M %z"),
                        window_start.strftime("%Y-%m-%d %H:%M %z"),
                    )
                )
            elif published > cutoff:
                errors.append(
                    "sources[%d]《%s》晚于本期截止时间：%s（截止 %s）"
                    % (
                        index,
                        label,
                        published.strftime("%Y-%m-%d %H:%M %z"),
                        cutoff.strftime("%Y-%m-%d %H:%M %z"),
                    )
                )
        except (TypeError, ValueError) as exc:
            errors.append("sources[%d]《%s》时间无效：%s" % (index, label, exc))

        used_in = item.get("used_in")
        if not isinstance(used_in, list) or not used_in:
            errors.append("sources[%d]《%s》缺少非空 used_in 数组" % (index, label))
            continue
        unknown = set(used_in) - section_ids
        if unknown:
            errors.append(
                "sources[%d]《%s》引用未知板块：%s"
                % (index, label, ", ".join(sorted(unknown)))
            )
        covered_sections.update(set(used_in) & section_ids)

    uncovered = section_ids - covered_sections
    if uncovered:
        errors.append("以下板块没有有效来源映射：%s" % ", ".join(sorted(uncovered)))
    if errors:
        sys.exit("✗ 时效校验失败：\n- " + "\n- ".join(errors))

    return "%s 至 %s，共 %d 条来源" % (
        window_start.strftime("%Y-%m-%d %H:%M"),
        cutoff.strftime("%Y-%m-%d %H:%M"),
        len(sources),
    )


def _walk_client_text(value, path):
    if isinstance(value, str):
        yield path, value
    elif isinstance(value, list):
        for index, item in enumerate(value):
            yield from _walk_client_text(item, "%s[%d]" % (path, index))
    elif isinstance(value, dict):
        for key, item in value.items():
            yield from _walk_client_text(item, "%s.%s" % (path, key))


def validate_client_facing_copy(content):
    """阻止内部取数、飞书内容管理和脚本机制进入客户成稿。"""
    date_key = str(content.get("date_yyyymmdd", ""))
    if date_key < DYNAMIC_AS_OF_REQUIRED_FROM and not content.get("as_of"):
        return "旧样本：未强制内部机制词校验"
    client_fields = {
        "main_title": content.get("main_title"),
        "alert": content.get("alert"),
        "overview_subtitle": content.get("overview_subtitle"),
        "index_note": content.get("index_note"),
        "sections": content.get("sections", []),
        "logic_chain": content.get("logic_chain", {}),
        "calendar": content.get("calendar", []),
        "qdii_date_label": content.get("qdii_date_label"),
    }
    errors = []
    for path, text in _walk_client_text(client_fields, "content"):
        for term in INTERNAL_MECHANISM_TERMS:
            if term.lower() in text.lower():
                errors.append("%s 包含内部机制词《%s》" % (path, term))
    if errors:
        sys.exit("✗ 客户成稿校验失败：\n- " + "\n- ".join(errors))
    return "客户成稿未暴露内部运行机制"


def validate_final_render(content, html):
    """成稿后、发布前的独立终检。

    这一层检查渲染完成的 HTML，阻止日期/星期错配、空星期和模板
    占位符进入公众号草稿；不依赖模型自检。
    """
    errors = []
    date_key = str(content.get("date_yyyymmdd", ""))
    try:
        brief_date = datetime.datetime.strptime(date_key, "%Y%m%d").date()
        expected_date_cn = "%d年%d月%d日（%s）" % (
            brief_date.year, brief_date.month, brief_date.day, WEEKDAYS[brief_date.weekday()]
        )
        if content.get("date_cn") != expected_date_cn:
            errors.append("date_cn 应为《%s》，实际为《%s》" % (
                expected_date_cn, content.get("date_cn", "")))
    except (TypeError, ValueError):
        errors.append("date_yyyymmdd 无法解析：%s" % date_key)

    visible = re.sub(r"<style.*?</style>|<script.*?</script>", "", html, flags=re.S | re.I)
    visible = re.sub(r"<[^>]+>", " ", visible)
    visible = re.sub(r"\s+", " ", visible)
    errors.extend(validate_no_structure_leaks(content, visible))
    for pattern, label in FINAL_PLACEHOLDER_PATTERNS:
        if re.search(pattern, visible, re.I):
            errors.append("成稿残留%s" % label)

    # 逐一核对成稿中所有“YYYY年M月D日（周X）”，包括简报日期和 QDII 基准日。
    dated_weekdays = re.findall(
        r"(\d{4})年(\d{1,2})月(\d{1,2})日（(周[^）]*)）", visible
    )
    if not dated_weekdays:
        errors.append("成稿未找到完整的“年月日（周几）”日期")
    for year, month, day, actual_weekday in dated_weekdays:
        label = "%s年%s月%s日" % (year, month, day)
        try:
            value = datetime.date(int(year), int(month), int(day))
        except ValueError:
            errors.append("成稿包含无效日期：%s" % label)
            continue
        expected_weekday = WEEKDAYS[value.weekday()]
        if actual_weekday != expected_weekday:
            errors.append("成稿日期星期不匹配：%s应为%s，实际为%s" % (
                label, expected_weekday, actual_weekday))

    if errors:
        sys.exit("✗ 成稿终检失败，禁止发布：\n- " + "\n- ".join(errors))
    return "日期/星期一致，无模板占位符，无程序结构残留"


def fmt_maxsg(n):
    """日限额格式化: 200→¥200/日, 1000→¥1,000/日, 10000→¥1万/日, 500000→¥50万/日。"""
    n = int(n)
    if n >= 10000 and n % 10000 == 0:
        return "¥%d万/日" % (n // 10000)
    if n >= 10000:
        return "¥%g万/日" % (n / 10000)
    return "¥%s/日" % format(n, ",")


def qdii_badge(status, maxsg):
    """申购状态 → 彩色 badge。限大额=红, 暂停申购=深红停购, 开放申购=绿不限额。"""
    if status == "暂停申购":
        return '<span style="font-size:10px;background-color:#7a1f1f;color:#fff;padding:1px 5px;border-radius:3px;font-weight:700;">停购</span>'
    if status == "开放申购":
        return '<span style="font-size:10px;background-color:#2f7d32;color:#fff;padding:1px 5px;border-radius:3px;font-weight:700;">不限额</span>'
    # 限大额(默认)
    return '<span style="font-size:10px;background-color:%s;color:#fff;padding:1px 5px;border-radius:3px;font-weight:700;">限额%s</span>' % (RED, fmt_maxsg(maxsg))


def _rows_from_snapshot(d):
    """快照 dict({funds:[{fcode,est_chg,sgzt,maxsg}]}) → (20 项 list, data_date)。"""
    m = {f["fcode"]: f for f in d.get("funds", [])}
    out = []
    for code, _name in QDII_FUNDS:
        f = m.get(code, {})
        chg = f.get("est_chg")
        if isinstance(chg, (int, float)):
            chg = "%+.2f%%" % chg
        elif chg is None or str(chg).strip() in ("", "None", "null"):
            chg = "—"   # 接口暂无估算 → 中性占位,不渲染成 0 或 None
        out.append({"pct": chg, "status": f.get("sgzt", "限大额"), "maxsg": f.get("maxsg", 0)})
    return out, d.get("data_date")


def load_qdii(content):
    """返回 (20 项 list, data_date, source)。
    取数优先级: 云端 HTTP 接口 → /tmp 易失缓存 → 项目内持久副本 → content['qdii']。
    若 content 含 qdii_manual:true 且 qdii 数组非空，则直接使用，跳过自动取数。"""
    # 0) 手动覆盖(截图校正时使用)：content 设置 qdii_manual:true 时直接用 content['qdii']
    if content.get("qdii_manual") and content.get("qdii"):
        return content["qdii"], None, "content"
    # 1) 云端常驻接口(首选)——成功时落一份持久副本供断网兜底
    try:
        import urllib.request
        with urllib.request.urlopen(QDII_API, timeout=10) as r:
            d = json.loads(r.read().decode("utf-8"))
        if d.get("funds"):
            try:
                json.dump(d, open(PROJECT_CACHE, "w", encoding="utf-8"),
                          ensure_ascii=False, indent=2)
            except Exception:
                pass
            rows, date = _rows_from_snapshot(d)
            return rows, date, "api"
    except Exception as e:
        print("[qdii] 云端接口取数失败,降级本地缓存: %s" % e, file=sys.stderr)
    # 2) 本机 /tmp 易失缓存
    if os.path.exists(QDII_CACHE):
        rows, date = _rows_from_snapshot(json.load(open(QDII_CACHE, encoding="utf-8")))
        return rows, date, "tmp"
    # 3) 项目内持久副本(上次云端成功的快照)
    if os.path.exists(PROJECT_CACHE):
        rows, date = _rows_from_snapshot(json.load(open(PROJECT_CACHE, encoding="utf-8")))
        return rows, date, "project"
    # 4) content 内嵌(最后兜底)
    return content.get("qdii", []), None, "content"


# ---------- 区块渲染 ----------
DIVIDER = '<p style="border-top:2px solid #2e2e2c;margin:28px 0 20px;"></p>\n'

def section_header(emoji, color, title):
    return ('<p style="font-size:16px;font-weight:800;color:#1a1a18;border-left:4px solid %s;'
            'padding-left:10px;margin:0 0 12px;">%s %s</p>\n') % (color, emoji, title)


def bullet(text, accent):
    return ('<p style="font-size:14px;line-height:1.9;margin:0 0 8px;">● %s</p>'
            % md(text, accent))


def note_bar(text, kind, accent):
    bg, border, color = NOTE_COLORS.get(kind, NOTE_COLORS["gray"])
    return ('<section style="background-color:%s;border-left:4px solid %s;border-radius:0 8px 8px 0;'
            'padding:11px 14px;margin:16px 0 4px;"><span style="display:block;font-size:13px;color:%s;'
            'font-weight:600;line-height:1.7;">💡 <strong>逻辑快读：</strong>%s</span></section>'
            % (bg, border, color, md(text, accent)))


def render_section(sec):
    sid = sec.get("id", "")
    dfl = SECTION_DEFAULTS.get(sid, {"emoji": "▪️", "color": RED, "note": "gray"})
    emoji = sec.get("emoji", dfl["emoji"])
    color = sec.get("color", dfl["color"])
    note_kind = sec.get("note_color", dfl["note"])
    html = [DIVIDER, section_header(emoji, color, sec["title"])]
    html += [bullet(b, color) for b in sec.get("bullets", [])]
    if sec.get("note"):
        html.append(note_bar(sec["note"], note_kind, color))
    return "".join(html)


def render_index_table(label, rows, price_label="收盘价"):
    out = ['<table style="width:100%;border-collapse:collapse;font-size:13px;margin:0 0 12px;">',
           '<tr><td width="40%%" style="background-color:#1a1a18;color:#f5f4f0;font-weight:700;padding:8px 10px;">%s</td>'
           '<td width="28%%" style="background-color:#1a1a18;color:#f5f4f0;font-weight:700;padding:8px 10px;">%s</td>'
           '<td width="32%%" style="background-color:#1a1a18;color:#f5f4f0;font-weight:700;padding:8px 10px;">涨跌幅</td></tr>' % (label, price_label)]
    for i, (name, r) in enumerate(zip(INDEX_NAMES, rows)):
        bg = "#fff" if i % 2 == 0 else "#fafafa"
        c = dir_color(r.get("dir", "up"))
        out.append('<tr><td style="padding:8px 10px;background-color:%s;">%s</td>'
                   '<td style="padding:8px 10px;background-color:%s;">%s</td>'
                   '<td style="padding:8px 10px;background-color:%s;color:%s;font-weight:700;">%s</td></tr>'
                   % (bg, name, bg, r["price"], bg, c, r["chg"]))
    out.append('</table>')
    return "\n".join(out)


def render_commodity_table(label, rows):
    out = ['<table style="width:100%;border-collapse:collapse;font-size:13px;margin:0 0 20px;">',
           '<tr><td width="40%%" style="background-color:#3a3835;color:#f5f4f0;font-weight:700;padding:8px 10px;">大宗 / 外汇（%s）</td>'
           '<td width="28%%" style="background-color:#3a3835;color:#f5f4f0;font-weight:700;padding:8px 10px;">价格</td>'
           '<td width="32%%" style="background-color:#3a3835;color:#f5f4f0;font-weight:700;padding:8px 10px;">涨跌</td></tr>' % label]
    for (name, bg), r in zip(COMMODITY_ROWS, rows):
        c = dir_color(r.get("dir", "up"))
        out.append('<tr><td style="padding:8px 10px;background-color:%s;">%s</td>'
                   '<td style="padding:8px 10px;background-color:%s;">%s</td>'
                   '<td style="padding:8px 10px;background-color:%s;color:%s;font-weight:700;">%s</td></tr>'
                   % (bg, name, bg, r["price"], bg, c, r["chg"]))
    out.append('</table>')
    return "\n".join(out)


def render_logic(lc):
    parts = []
    for p in lc["paths"]:
        sub = ('<br><span style="color:%s;font-size:12px;">%s</span>' % (p["color"], md(p["sub"], p["color"]))) if p.get("sub") else ""
        parts.append('<strong style="color:%s;">%s</strong><br>%s%s'
                     % (p["color"], p["name"], md(p["body"], p["color"]), sub))
    body = "<br><br>".join(parts)
    if lc.get("priority"):
        body += '<br><br><span style="font-size:12px;color:#888;">%s</span>' % md(lc["priority"])
    return ('<section style="background-color:#f5f4f0;border-radius:8px;padding:14px 16px;margin:0 0 20px;">'
            '<span style="display:block;font-size:13px;line-height:2.2;color:#1a1a18;">%s</span></section>' % body)


def render_calendar(rows):
    out = ['<table style="width:100%;border-collapse:collapse;font-size:13px;margin:0 0 20px;table-layout:fixed;">',
           '<tr><td width="100" style="background-color:#1a1a18;color:#f5f4f0;font-weight:700;padding:8px 10px;white-space:nowrap;word-break:keep-all;">日期</td>'
           '<td style="background-color:#1a1a18;color:#f5f4f0;font-weight:700;padding:8px 10px;">事件</td>'
           '<td width="60" style="background-color:#1a1a18;color:#f5f4f0;font-weight:700;padding:8px 10px;text-align:center;">重要性</td></tr>']
    for i, r in enumerate(rows):
        bg = "#fff" if i % 2 == 0 else "#fafafa"
        stars = "⭐" * int(r.get("stars", 1))
        out.append('<tr><td style="padding:8px 10px;background-color:%s;white-space:nowrap;word-break:keep-all;">%s</td>'
                   '<td style="padding:8px 10px;background-color:%s;">%s</td>'
                   '<td style="padding:8px 10px;background-color:%s;text-align:center;">%s</td></tr>'
                   % (bg, r["date"], bg, r["event"], bg, stars))
    out.append('</table>')
    return "\n".join(out)


def render_qdii_table(rows):
    out = ['<table style="width:100%;border-collapse:collapse;font-size:13px;margin:0 0 8px;border:1px solid #ece9e2;">']
    n = len(QDII_FUNDS)
    for i, ((code, name), r) in enumerate(zip(QDII_FUNDS, rows)):
        bg = "#fff" if i % 2 == 0 else "#fafafa"
        border = "" if i == n - 1 else "border-bottom:1px solid #ece9e2;"
        width_l = "width:65%;" if i == 0 else ""
        width_r = "width:35%;" if i == 0 else ""
        pct = r["pct"]; col = pct_color(pct)
        badge = qdii_badge(r.get("status", "限大额"), r.get("maxsg", 0))
        out.append('<tr><td style="padding:9px 10px;%sbackground-color:%s;%s">%s<br>'
                   '<span style="font-size:10px;background-color:#f0eee5;color:#666;padding:1px 5px;border-radius:3px;font-weight:700;">%s</span> %s</td>'
                   '<td style="padding:9px 10px;%sbackground-color:%s;text-align:right;color:%s;font-weight:700;%s">%s</td></tr>'
                   % (border, bg, width_l, name, code, badge, border, bg, col, width_r, pct))
    out.append('</table>')
    return "\n".join(out)


# ---------- 主渲染 ----------
def build(content):
    accent_focus = RED
    qdii_rows, cache_date, qdii_source = load_qdii(content)

    indices = render_index_table(
        content.get("index_date_label", ""),
        content["indices"],
        content.get("index_price_label", "收盘价"),
    )
    commodities = render_commodity_table(
        content.get("commodity_date_label", ""),
        content["commodities"],
    )
    sections = "\n".join(render_section(s) for s in content["sections"])
    logic = render_logic(content["logic_chain"])
    calendar = render_calendar(content["calendar"])
    qdii = render_qdii_table(qdii_rows)
    if content.get("as_of"):
        alert_display_time = _parse_source_time(content["as_of"]).strftime("%H:%M")
    else:
        alert_display_time = content["alert_time"]

    index_note = ""

    html = f"""<!DOCTYPE html>
<html lang="zh">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>股市马斯克 · 每日财经简报 {content['date_yyyymmdd']}</title>
</head>
<body style="font-family:-apple-system,BlinkMacSystemFont,'PingFang SC','Microsoft YaHei',sans-serif;background-color:transparent;color:#1a1a18;max-width:680px;margin:0 auto;padding:20px 16px 48px;font-size:15px;line-height:1.8;">

<button onclick="const r=document.createRange();r.selectNode(document.getElementById('article'));window.getSelection().removeAllRanges();window.getSelection().addRange(r);document.execCommand('copy');window.getSelection().removeAllRanges();this.textContent='✅ 已复制！请粘贴到公众号后台';setTimeout(()=>this.textContent='📋 复制正文（粘贴进公众号后台）',2500);"
        style="display:block;width:100%;padding:12px;background-color:#1a1a18;color:#f5f4f0;border:none;border-radius:0;font-size:14px;font-weight:700;cursor:pointer;margin-bottom:24px;">
  📋 复制正文（粘贴进公众号后台）
</button>

<div id="article">

<p style="text-align:center;font-size:11px;color:#bbb;letter-spacing:.8px;margin:0 0 10px;">股市马斯克 出品 · 美股 · 黄金 · 财经深度资讯</p>

<h1 style="font-size:18px;font-weight:800;color:#1a1a18;line-height:1.5;margin:0 0 10px;text-align:center;">{content['main_title']}</h1>

<p style="text-align:center;font-size:11px;color:#bbb;margin:0 0 24px;letter-spacing:.3px;">{content['date_cn']} · 过去24小时财经简报</p>

<section style="background-color:#FCEBEB;border-left:4px solid {RED};border-radius:0 8px 8px 0;padding:12px 14px;margin:0 0 22px;"><span style="display:block;font-size:13px;color:{RED};font-weight:600;line-height:1.7;">🚨 <strong style="color:{RED};">最新（北京时间{alert_display_time}）：</strong>{md(content['alert'])}</span></section>

{DIVIDER}<p style="font-size:16px;font-weight:800;color:#1a1a18;border-left:4px solid {RED};padding-left:10px;margin:0 0 12px;">📊 行情总览</p>

{indices}

{commodities}

{index_note}{sections}

{DIVIDER}<p style="font-size:16px;font-weight:800;color:#1a1a18;border-left:4px solid #1a1a18;padding-left:10px;margin:0 0 12px;">🧠 核心逻辑框架 | 当日因果传导链</p>
{logic}

{DIVIDER}<p style="font-size:16px;font-weight:800;color:#1a1a18;border-left:4px solid #1a1a18;padding-left:10px;margin:0 0 12px;">📅 本周财经日历</p>
{calendar}

{DIVIDER}<p style="font-size:16px;font-weight:800;color:#1a1a18;border-left:4px solid {GOLD};padding-left:10px;margin:0 0 4px;">📈 QDII 基金净值估算</p>
<p style="font-size:12px;color:#999;margin:0 0 10px;">{content['qdii_date_label']}</p>

{qdii}

<p style="font-size:11px;color:#999;line-height:1.6;margin:6px 0 20px;">⚠️ 以上为基于结构化行情与公开基金数据的估算结果，非实际净值，仅供参考；实际净值以各基金公司官方公告为准。数据来源：{PUBLIC_DATA_SOURCES}</p>

<p style="border-top:1px solid #ece9e2;margin:28px 0 16px;"></p>
<p style="text-align:center;font-size:12px;color:#bbb;line-height:2.4;">东财·雪球·支付宝·富途：<strong style="color:#333;font-size:14px;">股市马斯克</strong><br>微信公众号：<strong style="color:#333;font-size:14px;">中签课代表</strong><br>理财通：<strong style="color:#333;font-size:14px;">觉醒年代</strong></p>

</div>
</body>
</html>
"""
    return html, cache_date, qdii_source


def count_chars(html):
    """去 HTML 标签后的正文字符数(去空白)。"""
    t = re.sub(r"<[^>]+>", " ", html)
    t = re.sub(r"&[a-z]+;", " ", t)
    return len(re.sub(r"\s+", "", t))


def _plain_len(s):
    """content 文本去轻量标记/标签后的字符数(去空白)。"""
    s = re.sub(r"\[\[(?:red|green|c):(.*?)\]\]", r"\1", s or "")
    s = s.replace("**", "")
    s = re.sub(r"<[^>]+>", " ", s)
    return len(re.sub(r"\s+", "", s))


def section_counts(content):
    """按板块统计正文字数，超限时可一次定位砍哪。"""
    parts = [("alert", _plain_len(content.get("alert", "")))]
    for sec in content.get("sections", []):
        n = _plain_len(sec.get("title", "")) + _plain_len(sec.get("note", ""))
        n += sum(_plain_len(b) for b in sec.get("bullets", []))
        parts.append((sec["id"], n))
    lc = content.get("logic_chain", {})
    n = _plain_len(lc.get("priority", ""))
    n += sum(_plain_len(p.get("name", "")) + _plain_len(p.get("body", "")) + _plain_len(p.get("sub", ""))
             for p in lc.get("paths", []))
    parts.append(("logic", n))
    parts.append(("calendar", sum(_plain_len(c.get("date", "")) + _plain_len(c.get("event", ""))
                                  for c in content.get("calendar", []))))
    return parts


MARKET_SNAPSHOT = Path(os.path.dirname(os.path.abspath(__file__))) / "market_snapshot.json"
SNAPSHOT_MAX_AGE_H = 2


def fill_market(content):
    """content 缺 indices/commodities 时从 fetch_market.py 的快照自动填充。

    动态 as_of 时代禁止手填行情表。快照缺失、过期、无效、与本次
    content.as_of 不匹配，或任一行缺报价时间/市场状态/来源时直接退出。
    """
    date_key = str(content.get("date_yyyymmdd", ""))
    if "indices" in content or "commodities" in content:
        if date_key >= DYNAMIC_AS_OF_REQUIRED_FROM:
            sys.exit("✗ 行情校验失败：新流程禁止在 content JSON 手填 indices/commodities")
        if "indices" in content and "commodities" in content:
            return "旧样本 content.json 手填"
    try:
        snap = json.loads(MARKET_SNAPSHOT.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        sys.exit("✗ 行情快照缺失 —— 先运行 .venv/bin/python fetch_market.py")
    if snap.get("schema_version") != 2 or snap.get("valid") is not True:
        details = "; ".join(item.get("error", "") for item in snap.get("errors", []) if isinstance(item, dict))
        sys.exit("✗ 行情快照无效或版本过旧：%s —— 重新运行 fetch_market.py" % (details or "valid != true"))
    try:
        fetched_at = _parse_source_time(snap["fetched_at"])
        age = datetime.datetime.now(BRIEF_TZ) - fetched_at
        if age.total_seconds() > SNAPSHOT_MAX_AGE_H * 3600:
            sys.exit("✗ 行情快照已过期(%.1f小时前拉取) —— 重新运行 .venv/bin/python fetch_market.py" % (age.total_seconds() / 3600))
        if age.total_seconds() < -300:
            sys.exit("✗ 行情快照 fetched_at 晚于当前时间")
    except (KeyError, TypeError, ValueError):
        sys.exit("✗ 行情快照缺 fetched_at 字段 —— 重新运行 .venv/bin/python fetch_market.py")
    if "indices" not in snap or "commodities" not in snap:
        sys.exit("✗ 行情快照不含完整行情表 —— 重新运行 fetch_market.py")
    if not snap.get("commodity_date_label") or not snap.get("commodity_quote_date"):
        sys.exit("✗ 行情快照缺大宗/外汇实际报价日期 —— 重新运行 fetch_market.py")
    if snap.get("misaligned"):
        sys.exit("✗ 四个美股价格指数的最新交易日不一致，禁止自动构建")
    if "vix_lag_calendar_days" in snap:
        try:
            price_dates = {datetime.date.fromisoformat(row["session_date"]) for row in snap["indices"][:4]}
            vix_date = datetime.date.fromisoformat(snap["indices"][4]["session_date"])
            if len(price_dates) != 1:
                sys.exit("✗ 四个美股价格指数的最新交易日不一致，禁止自动构建")
            price_date = next(iter(price_dates))
            actual_lag = (price_date - vix_date).days
            allowed_lag = int(snap.get("vix_max_lag_calendar_days", 0))
            if actual_lag < 0 or actual_lag > allowed_lag:
                sys.exit("✗ VIX交易日与价格指数基准日不合规，禁止自动构建")
            if actual_lag != int(snap["vix_lag_calendar_days"]):
                sys.exit("✗ VIX交易日滞后元数据与行情行不一致，禁止自动构建")
            if snap.get("price_index_session_date") != price_date.isoformat():
                sys.exit("✗ 价格指数基准日元数据与行情行不一致，禁止自动构建")
            if snap.get("vix_session_date") != vix_date.isoformat():
                sys.exit("✗ VIX交易日元数据与行情行不一致，禁止自动构建")
        except (IndexError, KeyError, TypeError, ValueError):
            sys.exit("✗ VIX日期审计字段无效，禁止自动构建")
    if content.get("as_of"):
        try:
            content_as_of = _parse_source_time(content["as_of"])
            snapshot_as_of = _parse_source_time(snap["run_started_at"])
        except (KeyError, TypeError, ValueError) as exc:
            sys.exit("✗ 行情快照执行时间无效：%s" % exc)
        if content_as_of != snapshot_as_of:
            sys.exit(
                "✗ content.as_of 与行情快照不匹配：%s != %s"
                % (content_as_of.isoformat(), snapshot_as_of.isoformat())
            )
    for group_name in ("indices", "commodities"):
        for index, row in enumerate(snap[group_name], 1):
            missing = [field for field in ("quote_time", "market_state", "provider", "change_basis") if not row.get(field)]
            if missing:
                sys.exit("✗ 行情快照 %s[%d] 缺少审计字段：%s" % (group_name, index, ", ".join(missing)))
    content["indices"] = snap["indices"]
    content["commodities"] = snap["commodities"]
    content["index_date_label"] = snap["index_date_label"]
    content["index_price_label"] = snap["index_price_label"]
    content["commodity_date_label"] = snap["commodity_date_label"]
    return "行情快照 %s (as_of=%s, 拉取于 %s)" % (
        MARKET_SNAPSHOT,
        snap["run_started_at"],
        snap["fetched_at"],
    )


def main():
    if len(sys.argv) < 2:
        sys.exit("用法: .venv/bin/python build_brief.py content_YYYYMMDD.json [output.html]")
    content = json.load(open(sys.argv[1], encoding="utf-8"))
    structure_errors = validate_no_structure_leaks(content)
    if structure_errors:
        sys.exit("✗ 正文结构泄漏校验失败：\n- " + "\n- ".join(structure_errors))
    emphasis_errors = validate_emphasis(content)
    if emphasis_errors:
        sys.exit("✗ 强调色校验失败：\n- " + "\n- ".join(emphasis_errors))
    emphasis_status = emphasis_summary(content)
    client_copy_status = validate_client_facing_copy(content)
    freshness_status = validate_source_freshness(content)
    market_source = fill_market(content)
    html, cache_date, qdii_source = build(content)
    final_qa_status = validate_final_render(content, html)
    out = sys.argv[2] if len(sys.argv) > 2 else "wechat_article_%s.html" % content["date_yyyymmdd"]
    open(out, "w", encoding="utf-8").write(html)
    n = count_chars(html)
    flag = "✅ 达标" if 6000 <= n <= 7000 else ("⚠️ 偏少需扩充" if n < 6000 else "⚠️ 超限需精简")
    print("已生成: %s" % out)
    SRC_LABEL = {
        "api":     "云端接口 /api/qdii-snapshot",
        "tmp":     "本机缓存 /tmp/qdii_cache.json",
        "project": "项目持久副本 qdii_cache.json",
        "content": "content.json['qdii'] (兜底沿用)",
    }
    label = SRC_LABEL.get(qdii_source, qdii_source)
    print("行情表来源: %s" % market_source)
    print("客户成稿校验: %s" % client_copy_status)
    print("强调色校验: %s" % emphasis_status)
    print("成稿终检: %s" % final_qa_status)
    print("新闻时效窗口: %s" % freshness_status)
    print("QDII 数据来源: %s%s" % (label, " (data_date=%s)" % cache_date if cache_date else ""))
    print("正文字符数(去标签): %d  → %s  [硬约束 6000-7000]" % (n, flag))
    parts = section_counts(content)
    print("分段字数: " + " | ".join("%s %d" % p for p in parts))
    if n > 7000:
        over = n - 7000
        top = sorted((p for p in parts if p[0] not in ("calendar",)), key=lambda x: -x[1])[:3]
        hint = "、".join("%s(约-%d)" % (t[0], round(over * t[1] / sum(x[1] for x in top))) for t in top)
        print("→ 需精简约 %d 字，建议按占比砍: %s，一次砍到位后重跑" % (over, hint))
    elif n < 6000:
        print("→ 需扩充约 %d 字，优先加 ai_tech / focus 正文" % (6000 - n))


if __name__ == "__main__":
    main()
