#!/usr/bin/env python3
"""内容层共享校验：供写稿、构建和发布入口共同调用。"""

import datetime
import re


EMPHASIS_REQUIRED_FROM = "20260826"
# 财经日历改由 fetch_calendar.py 确定性回填后启用硬校验（历史样稿不追溯）。
CALENDAR_REQUIRED_FROM = "20260901"
CALENDAR_DATE_RE = re.compile(r"^(\d{1,2})月(\d{1,2})日\s*周([一二三四五六日])$")
CALENDAR_WEEKDAYS = "一二三四五六日"
CALENDAR_MIN_ROWS = 1
CALENDAR_MAX_ROWS = 6
CALENDAR_FORWARD_DAYS = 10
CALENDAR_EVENT_MAX = 60
COLOR_MARKER_RE = re.compile(r"\[\[(?:red|green|c):(.+?)\]\]")
COLOR_MARKER_KIND_RE = re.compile(r"\[\[(red|green|c):(.+?)\]\]")
COLOR_SPAN_MAX = 40
CORE_SECTIONS = {"focus", "ai_tech"}
# 客户成稿人名统一简称（用户规定）：写稿、构建时自动替换，发布前终检仍残留则拦截。
# sources 是来源登记（新闻原标题），不进入成稿正文，保持原样。
NAME_ABBREVIATIONS = {"习近平": "习"}
STRUCTURE_LEAK_RE = re.compile(
    r"\{\s*['\"][^'\"\r\n]{1,40}['\"]\s*:"
    r"|['\"]emphasis['\"]\s*:"
    r"|(?:^|\s)(?:dict|OrderedDict)\s*\("
    r"|<class\s+['\"]",
    re.I,
)


SUNDAY_MODE = "sunday_incremental"


def word_limits(content):
    """Sunday's explicitly selected incremental edition may be shorter."""
    if content.get("edition") != SUNDAY_MODE:
        return 5000, 7000
    try:
        day = datetime.datetime.strptime(content.get("date_yyyymmdd", ""), "%Y%m%d").date()
    except (ValueError, TypeError):
        raise ValueError("周日增量版日期无效")
    if day.weekday() != 6:
        raise ValueError("周日增量版仅能用于周日，不能放宽其他日期字数")
    return 4500, 6000


def apply_name_abbreviations(value, _key: str | None = None):
    """递归替换 content 中所有字符串里的全称为简称（跳过 sources），返回新对象。"""
    if _key == "sources":
        return value
    if isinstance(value, str):
        for full, short in NAME_ABBREVIATIONS.items():
            value = value.replace(full, short)
        return value
    if isinstance(value, list):
        return [apply_name_abbreviations(item) for item in value]
    if isinstance(value, dict):
        return {k: apply_name_abbreviations(v, k) for k, v in value.items()}
    return value


def name_abbreviation_errors(text: str) -> list[str]:
    return ["成稿仍含全称「%s」，应写作「%s」" % (full, short)
            for full, short in NAME_ABBREVIATIONS.items() if full in text]


def validate_no_structure_leaks(content: dict, rendered_text: str | None = None) -> list[str]:
    """Reject non-text body nodes and serialized dict/JSON fragments in client copy."""
    errors = []
    for section_index, section in enumerate(content.get("sections", []), 1):
        if not isinstance(section, dict):
            errors.append("sections[%d] 必须是对象" % section_index)
            continue
        sid = section.get("id") or section_index
        bullets = section.get("bullets")
        if not isinstance(bullets, list):
            errors.append("板块 %s bullets 必须是字符串数组" % sid)
            continue
        for bullet_index, bullet in enumerate(bullets, 1):
            if not isinstance(bullet, str):
                errors.append("板块 %s bullet[%d] 必须是纯文本字符串，实际为 %s" % (
                    sid, bullet_index, type(bullet).__name__))
            elif STRUCTURE_LEAK_RE.search(bullet):
                errors.append("板块 %s bullet[%d] 含 Python/JSON 对象结构残留" % (
                    sid, bullet_index))
    for field in ("main_title", "alert", "index_note", "qdii_date_label"):
        value = content.get(field)
        if isinstance(value, str) and STRUCTURE_LEAK_RE.search(value):
            errors.append("字段 %s 含 Python/JSON 对象结构残留" % field)
    if isinstance(rendered_text, str) and STRUCTURE_LEAK_RE.search(rendered_text):
        errors.append("HTML 可见正文含 Python/JSON 程序结构残留")
    return errors


def _add_one_context_marker(text: str) -> tuple[str, bool]:
    """Wrap one existing, unmarked clause in [[c:...]] without changing its text."""
    occupied = [
        match.span()
        for match in re.finditer(r"\[\[(?:red|green|c):.+?\]\]|\*\*.+?\*\*", text)
    ]
    candidates = []
    for match in re.finditer(r"(?:^|[，。；：！？])([^，。；：！？]+)", text):
        start, end = match.span(1)
        segment = match.group(1)
        left_trim = len(segment) - len(segment.lstrip())
        start += left_trim
        segment = segment.strip()
        if not segment or any(start < stop and end > begin for begin, stop in occupied):
            continue
        leading = re.match(r"^(与此同时|同时|此外|因此|不过|但是|但|若|如果|当前|其中|反之|这意味着)", segment)
        if leading and len(segment) - leading.end() >= 4:
            start += leading.end()
            segment = segment[leading.end():]
        candidate = segment[:32].rstrip()
        if len(candidate) < 4 or "[[" in candidate or "]]" in candidate or "**" in candidate:
            continue
        score = (5 if re.search(r"\d", candidate) else 0) + (3 if re.search(r"[A-Za-z]{2,}", candidate) else 0)
        score += min(len(candidate), 24) / 24
        candidates.append((score, -start, start, start + len(candidate)))
    if not candidates:
        return text, False
    _, _, start, end = max(candidates)
    return text[:start] + "[[c:" + text[start:end] + "]]" + text[end:], True


def ensure_emphasis(content: dict) -> int:
    """Deterministically add missing context-color spans around existing wording.

    This is a formatting normalization step: it never adds, removes, or rewrites
    visible copy. Directional red/green markers remain the model/editor's job;
    automatic additions always use the section context color.
    """
    if str(content.get("date_yyyymmdd", "")) < EMPHASIS_REQUIRED_FROM:
        return 0
    added = 0

    def fill(text: str, minimum: int) -> str:
        nonlocal added
        while len(COLOR_MARKER_RE.findall(text)) < minimum:
            text, changed = _add_one_context_marker(text)
            if not changed:
                break
            added += 1
        return text

    if isinstance(content.get("alert"), str):
        content["alert"] = fill(content["alert"], 2)
    for section in content.get("sections", []):
        if not isinstance(section, dict) or not isinstance(section.get("bullets"), list):
            continue
        target = 2 if section.get("id") in CORE_SECTIONS else 1
        section["bullets"] = [
            fill(bullet, target) if isinstance(bullet, str) else bullet
            for bullet in section["bullets"]
        ]
    return added


def trim_long_emphasis(content: dict) -> int:
    """把超过 COLOR_SPAN_MAX 的强调色 span 收窄到上限内：只保留前 40 字着色，
    其余文字解出标记但保持可见——不删除、不改写任何可见正文，仅缩短着色范围。
    避免模型偶发的“整句着色”因单处超 40 字触发整篇重试。幂等。"""
    trimmed = 0

    def fix(text: str) -> str:
        nonlocal trimmed

        def repl(match):
            nonlocal trimmed
            kind, inner = match.group(1), match.group(2)
            if len(inner) <= COLOR_SPAN_MAX:
                return match.group(0)
            head = inner[:COLOR_SPAN_MAX]
            cut = max((head.rfind(p) for p in "，。；：、,.;:"), default=-1)
            if cut >= 4:
                head, tail = inner[:cut + 1], inner[cut + 1:]
            else:
                head, tail = inner[:COLOR_SPAN_MAX], inner[COLOR_SPAN_MAX:]
            trimmed += 1
            return "[[%s:%s]]%s" % (kind, head, tail)

        return COLOR_MARKER_KIND_RE.sub(repl, text)

    if isinstance(content.get("alert"), str):
        content["alert"] = fix(content["alert"])
    for section in content.get("sections", []):
        if not isinstance(section, dict) or not isinstance(section.get("bullets"), list):
            continue
        section["bullets"] = [fix(b) if isinstance(b, str) else b for b in section["bullets"]]
    return trimmed


def validate_emphasis(content: dict) -> list[str]:
    """返回强调色覆盖错误；历史样稿不追溯强制。"""
    if str(content.get("date_yyyymmdd", "")) < EMPHASIS_REQUIRED_FROM:
        return []

    errors = []
    alert = content.get("alert")
    if isinstance(alert, str):
        alert_markers = COLOR_MARKER_RE.findall(alert)
        if len(alert_markers) < 2:
            errors.append("alert 至少需要 2 处强调色，实际 %d 处" % len(alert_markers))
        for marker in alert_markers:
            if len(marker) > COLOR_SPAN_MAX:
                errors.append("alert 单处强调色不得超过 %d 字，实际 %d 字" % (
                    COLOR_SPAN_MAX, len(marker)))

    for section in content.get("sections", []):
        if not isinstance(section, dict):
            continue
        sid = section.get("id")
        bullets = section.get("bullets")
        if not isinstance(bullets, list):
            continue
        for bullet_index, bullet in enumerate(bullets, 1):
            if not isinstance(bullet, str):
                continue
            markers = COLOR_MARKER_RE.findall(bullet)
            if not markers:
                errors.append("板块 %s bullet[%d] 至少需要 1 处强调色" % (
                    sid, bullet_index))
            for marker in markers:
                if len(marker) > COLOR_SPAN_MAX:
                    errors.append(
                        "板块 %s bullet[%d] 单处强调色不得超过 %d 字，实际 %d 字" % (
                            sid, bullet_index, COLOR_SPAN_MAX, len(marker)))
        if sid in CORE_SECTIONS:
            marker_total = sum(
                len(COLOR_MARKER_RE.findall(bullet))
                for bullet in bullets if isinstance(bullet, str)
            )
            minimum = len(bullets) * 2
            if marker_total < minimum:
                errors.append("板块 %s 强调色至少 %d 处，实际 %d 处" % (
                    sid, minimum, marker_total))
    return errors


def emphasis_summary(content: dict) -> str:
    """生成构建/发布日志使用的简短覆盖摘要。"""
    alert_count = len(COLOR_MARKER_RE.findall(str(content.get("alert", ""))))
    bullets = [
        bullet
        for section in content.get("sections", []) if isinstance(section, dict)
        for bullet in section.get("bullets", []) if isinstance(bullet, str)
    ]
    total = alert_count + sum(len(COLOR_MARKER_RE.findall(bullet)) for bullet in bullets)
    colored = sum(bool(COLOR_MARKER_RE.search(bullet)) for bullet in bullets)
    return "强调色 %d 处，正文覆盖 %d/%d 条" % (total, colored, len(bullets))


def _calendar_entry_date(entry: dict, brief_date: datetime.date) -> tuple[datetime.date | None, str]:
    """解析日历条目日期：优先 date_iso，其次由“X月X日 周X”推年（跨年顺延）。"""
    iso = str(entry.get("date_iso") or "").strip()
    label = str(entry.get("date") or "").strip()
    matched = CALENDAR_DATE_RE.match(label)
    if not matched:
        return None, "日期必须写成“9月4日 周五”，实际为《%s》" % label
    month, day, weekday = int(matched.group(1)), int(matched.group(2)), matched.group(3)
    value = None
    if iso:
        try:
            value = datetime.date.fromisoformat(iso)
        except ValueError:
            return None, "date_iso 无效：%s" % iso
        if (value.month, value.day) != (month, day):
            return None, "date_iso %s 与日期《%s》不一致" % (iso, label)
    else:
        for year in (brief_date.year, brief_date.year + 1):
            try:
                candidate = datetime.date(year, month, day)
            except ValueError:
                return None, "日期不存在：%s" % label
            if candidate >= brief_date:
                value = candidate
                break
        if value is None:
            return None, "日期不存在：%s" % label
    if CALENDAR_WEEKDAYS[value.weekday()] != weekday:
        return None, "日期星期错配：%s 实际为周%s" % (label, CALENDAR_WEEKDAYS[value.weekday()])
    return value, ""


def validate_calendar(content: dict) -> list[str]:
    """本周财经日历硬校验：非空、格式统一、星期与真实日历一致、在展望窗口内且升序。

    日历此前由模型自由生成，出现过整表为空和把下周非农写成当天（2026-08-29~08-31）。
    条目现由 fetch_calendar.py 确定性回填，这里做最后一道拦截。
    """
    date_key = str(content.get("date_yyyymmdd", ""))
    if date_key < CALENDAR_REQUIRED_FROM:
        return []
    try:
        brief_date = datetime.datetime.strptime(date_key, "%Y%m%d").date()
    except ValueError:
        return ["date_yyyymmdd 无法解析，无法校验财经日历：%s" % date_key]

    rows = content.get("calendar")
    if not isinstance(rows, list):
        return ["calendar 必须是数组"]
    if len(rows) < CALENDAR_MIN_ROWS:
        return ["本周财经日历为空 —— 先运行 fetch_calendar.py 生成 calendar_feed.json"]
    if len(rows) > CALENDAR_MAX_ROWS:
        return ["本周财经日历最多 %d 条，实际 %d 条" % (CALENDAR_MAX_ROWS, len(rows))]

    errors = []
    previous = None
    for index, entry in enumerate(rows, 1):
        if not isinstance(entry, dict):
            errors.append("calendar[%d] 必须是对象" % index)
            continue
        event = entry.get("event")
        if not isinstance(event, str) or not event.strip():
            errors.append("calendar[%d] 缺少事件描述" % index)
        elif len(event) > CALENDAR_EVENT_MAX:
            errors.append("calendar[%d] 事件描述超过 %d 字" % (index, CALENDAR_EVENT_MAX))
        stars = entry.get("stars")
        if not isinstance(stars, int) or isinstance(stars, bool) or not 1 <= stars <= 5:
            errors.append("calendar[%d] stars 必须是 1-5 的整数" % index)
        value, problem = _calendar_entry_date(entry, brief_date)
        if problem:
            errors.append("calendar[%d] %s" % (index, problem))
            continue
        if value < brief_date:
            errors.append("calendar[%d] %s 早于简报日期，禁止回填过期事件" % (index, entry.get("date")))
        elif (value - brief_date).days > CALENDAR_FORWARD_DAYS:
            errors.append("calendar[%d] %s 超出 %d 天展望窗口" % (
                index, entry.get("date"), CALENDAR_FORWARD_DAYS))
        if previous and value < previous:
            errors.append("calendar[%d] %s 未按日期升序排列" % (index, entry.get("date")))
        previous = value
    return errors


def calendar_summary(content: dict) -> str:
    rows = content.get("calendar")
    if not isinstance(rows, list) or not rows:
        return "无条目"
    return "%d 条（%s 起）" % (len(rows), rows[0].get("date", "?"))
