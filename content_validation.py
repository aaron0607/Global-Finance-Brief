#!/usr/bin/env python3
"""内容层共享校验：供写稿、构建和发布入口共同调用。"""

import re


EMPHASIS_REQUIRED_FROM = "20260826"
COLOR_MARKER_RE = re.compile(r"\[\[(?:red|green|c):(.+?)\]\]")
COLOR_SPAN_MAX = 40
CORE_SECTIONS = {"focus", "ai_tech"}
STRUCTURE_LEAK_RE = re.compile(
    r"\{\s*['\"][^'\"\r\n]{1,40}['\"]\s*:"
    r"|['\"]emphasis['\"]\s*:"
    r"|(?:^|\s)(?:dict|OrderedDict)\s*\("
    r"|<class\s+['\"]",
    re.I,
)


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
