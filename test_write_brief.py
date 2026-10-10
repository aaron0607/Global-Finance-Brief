import unittest
import datetime as dt
import re
from unittest.mock import patch

from write_brief import (
    MIN_BULLET_CHARS,
    NORMAL_PROFILE,
    THIN_PROFILE,
    WEEKDAYS,
    ALERT_MAX,
    ALERT_MIN,
    _expected_prev_us_session,
    build_prompt,
    build_repair_prompt,
    compress_bullets,
    ensure_alert_ceiling,
    ensure_length_floors,
    ensure_main_title,
    ensure_note_floor,
    fallback_main_title,
    is_thin_session,
    normalize_main_title,
    sanitize_content,
    title_news_errors,
    title_ok,
    validate_required,
    visible_text_length,
)
from content_validation import (COLOR_MARKER_RE, COLOR_SPAN_MAX, ensure_emphasis,
                                trim_long_emphasis, validate_no_structure_leaks)


class WeekdayMappingTests(unittest.TestCase):
    def test_august_14_2026_is_friday(self):
        value = dt.date(2026, 8, 14)
        self.assertEqual(WEEKDAYS[value.weekday()], "周五")

    def test_august_13_2026_is_thursday(self):
        value = dt.date(2026, 8, 13)
        self.assertEqual(WEEKDAYS[value.weekday()], "周四")


class MainTitleNormalizationTests(unittest.TestCase):
    def test_short_two_line_title_is_preserved(self):
        title = normalize_main_title("AI算力回调<br>黄金走强")
        self.assertEqual(title, "AI算力回调<br>黄金走强")
        self.assertTrue(title_ok(title))

    def test_valid_long_two_line_title_is_preserved(self):
        original = "AI上游周期延续<br>利率与油价再定价"
        self.assertEqual(normalize_main_title(original), original)
        self.assertTrue(title_ok(original))

    def test_invalid_long_line_is_balanced(self):
        original = "算力芯片产业周期延续利率与油价再定价全球科技资产同步反弹"
        title = normalize_main_title(original)
        self.assertTrue(title_ok(title))
        self.assertEqual(title.replace("<br>", ""), original)

    def test_overlong_title_is_not_truncated(self):
        original = "算" * 49
        self.assertEqual(normalize_main_title(original), original)
        self.assertFalse(title_ok(original))

    def test_each_title_line_may_hold_24_characters(self):
        title = "算" * 24 + "<br>" + "力" * 24
        self.assertTrue(title_ok(title))


class NewsTitleStyleTests(unittest.TestCase):
    def test_concrete_news_title_passes(self):
        self.assertEqual(title_news_errors("美股科技股反弹，布油上涨"), [])

    def test_company_action_and_market_reaction_pass(self):
        self.assertEqual(title_news_errors("英伟达加码电力，费半大涨"), [])

    def test_vague_investment_title_is_rejected(self):
        errors = title_news_errors("科技回调黄金蓄势")
        self.assertTrue(any("蓄势" in error for error in errors))

    def test_suspense_title_is_rejected(self):
        errors = title_news_errors("油金谁主沉浮？")
        self.assertTrue(any("谁主沉浮" in error for error in errors))
        self.assertTrue(any("问句" in error for error in errors))

    def test_abstract_summary_without_factual_verb_is_rejected(self):
        errors = title_news_errors("科技与黄金成为焦点")
        self.assertTrue(any("事实动词" in error for error in errors))

    def test_common_factual_verbs_pass(self):
        self.assertEqual(title_news_errors("CoreWeave营收翻倍"), [])

    def test_legacy_style_factual_verbs_pass(self):
        title = "特斯拉暴跌14%·英特尔盘后逆袭<br>关税落地油价破百·黄金失守4050"
        self.assertEqual(title_news_errors(title), [])


class MainTitleRepairTests(unittest.TestCase):
    SNAP = {
        "indices": [
            {"name": "标普500", "chg": "-0.32%"},
            {"name": "纳斯达克综合", "chg": "-0.60%"},
            {"name": "费城半导体", "chg": "+0.87%"},
        ]
    }
    POOL = [{"title": "CoreWeave营收同比增长112%", "summary": "盘后大涨16%"}]

    def test_market_fallback_is_valid(self):
        title = fallback_main_title(self.SNAP)
        self.assertEqual(title, "费半上涨")
        self.assertTrue(title_ok(title))
        self.assertEqual(title_news_errors(title), [])

    @patch("write_brief.call_deepseek")
    def test_invalid_title_is_repaired_without_rewriting_body(self, call):
        call.return_value = ('{"main_title":"CoreWeave营收翻倍<br>盘后大涨16%"}', "stop")
        content = {
            "main_title": "AI需求与资本开支成为焦点",
            "alert": "CoreWeave营收同比增长112%，盘后大涨16%。",
            "sections": [{"id": "focus", "title": "头号焦点 | CoreWeave财报"}],
        }
        note = ensure_main_title(content, self.SNAP, self.POOL)
        self.assertIn("已单独修复", note)
        self.assertEqual(content["main_title"], "CoreWeave营收翻倍<br>盘后大涨16%")

    @patch("write_brief.call_deepseek")
    def test_failed_title_repair_uses_market_fallback(self, call):
        call.return_value = ('{"main_title":"市场静待方向"}', "stop")
        content = {
            "main_title": "AI需求与资本开支成为焦点",
            "alert": "摘要",
            "sections": [],
        }
        note = ensure_main_title(content, self.SNAP, self.POOL)
        self.assertIn("行情兜底", note)
        self.assertEqual(content["main_title"], "费半上涨")


class ContentRepairTests(unittest.TestCase):
    def _valid_content(self):
        def section(sid, count):
            markers = "[[c:关键判断]][[red:关键数据]]" if sid in ("focus", "ai_tech") else "[[c:关键判断]]"
            return {"id": sid, "title": sid, "bullets": [markers + "x" * 240] * count,
                    "note": "n" * 50}
        return {
            "date_yyyymmdd": "20260826", "date_cn": "date", "as_of": "asof",
            "alert_time": "08:20", "alert": "[[red:科技反弹]][[green:油价回落]]" + "a" * 180,
            "index_note": "index",
            "qdii_date_label": "qdii", "sources": [{"source": "s"}], "calendar": [],
            "logic_chain": {
                "paths": [
                    {"name": "A", "color": "c", "body": "b", "sub": "s"},
                    {"name": "B", "color": "c", "body": "b", "sub": "s"},
                    {"name": "C", "color": "c", "body": "b", "sub": "s"},
                ],
                "priority": "p",
            },
            "sections": [
                section("focus", 3), section("ai_tech", 4),
                section("stock_rotation", 2), section("macro", 3),
                section("commodity", 2),
            ],
        }

    def test_full_schema_passes(self):
        self.assertEqual(validate_required(self._valid_content(), "20260826"), [])

    def test_wrong_order_and_bullet_count_are_reported(self):
        content = self._valid_content()
        content["sections"][0], content["sections"][1] = content["sections"][1], content["sections"][0]
        content["sections"][-1]["bullets"].append("extra")
        errors = validate_required(content, "20260826")
        self.assertTrue(any("顺序/集合" in error for error in errors))
        self.assertTrue(any("commodity bullets" in error for error in errors))

    def test_logic_priority_must_be_a_string(self):
        content = self._valid_content()
        content["logic_chain"]["priority"] = ["财报优先", "其次看PCE"]
        errors = validate_required(content, "20260826")
        self.assertTrue(any("priority 应为非空字符串" in error for error in errors))

    def test_short_argument_blocks_are_reported(self):
        content = self._valid_content()
        content["sections"][1]["bullets"][0] = "太短"
        content["sections"][1]["note"] = "太短"
        content["alert"] = "太短"
        errors = validate_required(content, "20260826")
        self.assertTrue(any("alert 可见正文应为" in error for error in errors))
        self.assertTrue(any("ai_tech bullet[1] 可见正文至少" in error for error in errors))
        self.assertTrue(any("ai_tech note 应为" in error for error in errors))

    def test_repair_prompt_contains_candidate_and_exact_feedback(self):
        content = self._valid_content()
        prompt = build_repair_prompt(
            content, "正文 5200 字，需增加 1200 字", [], "20260826", "date",
            "08:20", "2026-08-26T08:20:00+08:00", "index", "qdii",
        )
        self.assertIn("上一版 JSON", prompt)
        self.assertIn("正文 5200 字，需增加 1200 字", prompt)
        self.assertIn('\"sections\"', prompt)

    def test_missing_color_emphasis_is_reported(self):
        content = self._valid_content()
        content["alert"] = "a" * 200
        content["sections"][2]["bullets"][0] = "x" * 240
        errors = validate_required(content, "20260826")
        self.assertTrue(any("alert 至少需要 2 处强调色" in error for error in errors))
        self.assertTrue(any("stock_rotation bullet[1] 至少需要 1 处强调色" in error for error in errors))

    def test_core_sections_require_dense_emphasis(self):
        content = self._valid_content()
        content["sections"][0]["bullets"] = ["[[c:关键判断]]" + "x" * 240] * 3
        errors = validate_required(content, "20260826")
        self.assertTrue(any("板块 focus 强调色至少 6 处" in error for error in errors))

    def test_oversized_color_span_is_reported(self):
        content = self._valid_content()
        content["sections"][2]["bullets"][0] = "[[c:" + "长" * 41 + "]]" + "x" * 240
        errors = validate_required(content, "20260826")
        self.assertTrue(any("单处强调色不得超过 40 字" in error for error in errors))

    def test_emphasis_is_filled_without_changing_visible_copy(self):
        content = self._valid_content()
        content["alert"] = "**过去24小时核心变化**：英伟达公布财报，费半上涨，黄金回落，市场等待通胀数据。" + "摘要" * 71
        for section in content["sections"]:
            section["bullets"] = [
                "英伟达公布财报，半导体指数上涨，云厂商资本开支仍是后续验证条件。" + "产业链维持景气。" * 25
                for _ in section["bullets"]
            ]
        before = re.sub(r"\[\[(?:red|green|c):(.+?)\]\]", r"\1", str(content))
        added = ensure_emphasis(content)
        after = re.sub(r"\[\[(?:red|green|c):(.+?)\]\]", r"\1", str(content))
        self.assertGreater(added, 0)
        self.assertEqual(before, after)
        self.assertEqual(validate_required(content, "20260826"), [])
        self.assertEqual(ensure_emphasis(content), 0)
        self.assertTrue(all(
            len(marker) <= 40
            for section in content["sections"]
            for bullet in section["bullets"]
            for marker in COLOR_MARKER_RE.findall(bullet)
        ))

    def test_thirty_character_note_is_accepted_as_hard_floor(self):
        content = self._valid_content()
        content["sections"][0]["note"] = "结" * 30
        self.assertEqual(validate_required(content, "20260826"), [])

    def test_length_checks_ignore_lightweight_markup(self):
        content = self._valid_content()
        content["alert"] = "[[c:" + "导" * 90 + "]]" + "[[red:" + "读" * 90 + "]]"
        self.assertEqual(visible_text_length(content["alert"]), 180)
        errors = validate_required(content, "20260826")
        self.assertFalse(any("alert 可见正文应为" in error for error in errors))

    def test_bullet_hard_floor_only_rejects_fragments(self):
        content = self._valid_content()
        content["sections"][1]["bullets"][0] = "[[c:关键判断]][[red:关键数据]]" + "论" * 92
        self.assertEqual(visible_text_length(content["sections"][1]["bullets"][0]), 100)
        self.assertFalse(any(
            "ai_tech bullet[1] 可见正文至少" in error
            for error in validate_required(content, "20260826")
        ))
        content["sections"][1]["bullets"][0] = "[[c:关键判断]][[red:关键数据]]" + "论" * 91
        self.assertTrue(any(
            "ai_tech bullet[1] 可见正文至少 100 字，实际 99 字" in error
            for error in validate_required(content, "20260826")
        ))

    def test_model_bullet_object_extracts_text_without_serializing_dict(self):
        content = self._valid_content()
        text = "正文" * 60 + "[[c:核心结论]]"
        content["sections"][0]["bullets"][0] = {
            "text": text,
            "emphasis": "[[c:核心结论]]",
        }
        sanitize_content(content)
        bullet = content["sections"][0]["bullets"][0]
        self.assertEqual(bullet, text)
        self.assertNotIn("'text'", bullet)
        self.assertNotIn("'emphasis'", bullet)

    def test_non_text_bullet_and_serialized_object_are_rejected(self):
        content = self._valid_content()
        content["sections"][0]["bullets"][0] = {"unexpected": "value"}
        self.assertTrue(any("必须是纯文本字符串" in e for e in validate_no_structure_leaks(content)))
        content["sections"][0]["bullets"][0] = "{'text': '正文', 'emphasis': '重点'}"
        self.assertTrue(any("结构残留" in e for e in validate_no_structure_leaks(content)))
        content["sections"][0]["bullets"][0] = '{"body": "正文", "highlights": ["重点"]}'
        self.assertTrue(any("对象结构残留" in e for e in validate_no_structure_leaks(content)))
        content["sections"][0]["bullets"][0] = "OrderedDict([('body', '正文')])"
        self.assertTrue(any("对象结构残留" in e for e in validate_no_structure_leaks(content)))

    def test_short_notes_receive_section_specific_floor_once(self):
        content = self._valid_content()
        for section in content["sections"]:
            section["note"] = "结论过短"
        self.assertEqual(ensure_note_floor(content), 5)
        self.assertTrue(all(len(section["note"]) >= 30 for section in content["sections"]))
        snapshot = [section["note"] for section in content["sections"]]
        self.assertEqual(ensure_note_floor(content), 0)
        self.assertEqual(snapshot, [section["note"] for section in content["sections"]])


class ThinSessionTests(unittest.TestCase):
    def test_expected_prev_session_weekday_math(self):
        # 周二至周五 → 前一天
        self.assertEqual(_expected_prev_us_session(dt.date(2026, 9, 8)), dt.date(2026, 9, 7))
        # 周一 → 上周五
        self.assertEqual(_expected_prev_us_session(dt.date(2026, 9, 7)), dt.date(2026, 9, 4))
        # 周日 → 周五；周六 → 周五
        self.assertEqual(_expected_prev_us_session(dt.date(2026, 9, 6)), dt.date(2026, 9, 4))
        self.assertEqual(_expected_prev_us_session(dt.date(2026, 9, 5)), dt.date(2026, 9, 4))

    def test_post_holiday_tuesday_is_thin(self):
        # 劳动节后周二：最近美股交易日停在上周五，落后于预期的周一
        snap = {"price_index_session_date": "2026-09-04"}
        self.assertTrue(is_thin_session(snap, dt.date(2026, 9, 8)))

    def test_normal_tuesday_is_not_thin(self):
        snap = {"price_index_session_date": "2026-09-07"}
        self.assertFalse(is_thin_session(snap, dt.date(2026, 9, 8)))

    def test_normal_monday_after_weekend_is_not_thin(self):
        snap = {"price_index_session_date": "2026-09-04"}
        self.assertFalse(is_thin_session(snap, dt.date(2026, 9, 7)))

    def test_missing_or_bad_session_date_is_not_thin(self):
        self.assertFalse(is_thin_session({}, dt.date(2026, 9, 8)))
        self.assertFalse(is_thin_session({"price_index_session_date": "n/a"}, dt.date(2026, 9, 8)))


class ThinProfileValidationTests(unittest.TestCase):
    def _content_with_ai_tech_bullets(self, count, filler_chars):
        markers = "[[c:关键判断]][[red:关键数据]]"
        content = {
            "date_yyyymmdd": "20260908", "date_cn": "date", "as_of": "asof",
            "alert_time": "08:20", "alert": "[[red:科技反弹]][[green:油价回落]]" + "a" * 180,
            "index_note": "index", "qdii_date_label": "qdii",
            "sources": [{"source": "s"}], "calendar": [],
            "logic_chain": {"paths": [{"name": "A", "color": "c", "body": "b", "sub": "s"}] * 3,
                            "priority": "p"},
            "sections": [
                {"id": "focus", "title": "t", "bullets": [markers + "x" * 240] * 3, "note": "n" * 50},
                {"id": "ai_tech", "title": "t", "bullets": [markers + "论" * filler_chars] * count, "note": "n" * 50},
                {"id": "stock_rotation", "title": "t", "bullets": ["[[c:判断]]" + "x" * 240] * 2, "note": "n" * 50},
                {"id": "macro", "title": "t", "bullets": ["[[c:判断]]" + "x" * 240] * 3, "note": "n" * 50},
                {"id": "commodity", "title": "t", "bullets": ["[[c:判断]]" + "x" * 240] * 2, "note": "n" * 50},
            ],
        }
        return content

    def test_thin_profile_allows_three_ai_tech_bullets(self):
        content = self._content_with_ai_tech_bullets(3, 240)
        # 正常档 ai_tech 下限为 4 条 → 报错
        self.assertTrue(any("ai_tech bullets" in e for e in validate_required(content, "20260908")))
        # 淡日档 ai_tech 下限为 3 条 → 通过
        self.assertEqual(
            [e for e in validate_required(content, "20260908",
                                          THIN_PROFILE["bullet_rules"], THIN_PROFILE["min_bullet_chars"])
             if "ai_tech bullets" in e], [])

    def test_thin_profile_relaxes_bullet_char_floor_to_90(self):
        # 90 字可见正文：正常档（下限100）报错，淡日档（下限90）通过
        content = self._content_with_ai_tech_bullets(4, 90 - 8)  # markers 可见 8 字 → 可见合计 90
        self.assertEqual(visible_text_length(content["sections"][1]["bullets"][0]), 90)
        self.assertTrue(any("ai_tech bullet[1] 可见正文至少 100" in e
                            for e in validate_required(content, "20260908")))
        self.assertFalse(any("ai_tech bullet[1] 可见正文至少" in e
                             for e in validate_required(content, "20260908",
                                                        THIN_PROFILE["bullet_rules"],
                                                        THIN_PROFILE["min_bullet_chars"])))


class ThinPromptTests(unittest.TestCase):
    SNAP = {"news_window": {"start": "2026-09-07T08:20:01+08:00", "end": "2026-09-08T08:20:01+08:00"},
            "indices": [], "commodities": [], "extra_quotes": []}

    def _build(self, profile):
        return build_prompt(self.SNAP, [], "20260908", "date", "08:20",
                            "2026-09-08T08:20:01+08:00", "index", "qdii", "", None, [], profile)

    def test_thin_prompt_injects_notice_and_relaxed_numbers(self):
        prompt = self._build(THIN_PROFILE)
        self.assertIn("淡日提示", prompt)
        self.assertIn("%d-%d 字" % (THIN_PROFILE["target_min"], THIN_PROFILE["target_max"]), prompt)

    def test_normal_prompt_has_no_thin_notice(self):
        prompt = self._build(NORMAL_PROFILE)
        self.assertNotIn("淡日提示", prompt)
        self.assertIn("%d-%d 字" % (NORMAL_PROFILE["target_min"], NORMAL_PROFILE["target_max"]), prompt)


class DeterministicNormalizerTests(unittest.TestCase):
    def test_trim_long_emphasis_shrinks_span_keeps_visible_text(self):
        long_inner = "关键判断" * 13  # 52 字 > 40 上限
        content = {"alert": "", "sections": [
            {"id": "ai_tech", "bullets": ["[[c:%s]]收尾" % long_inner]}]}
        before_visible = visible_text_length(content["sections"][0]["bullets"][0])
        self.assertEqual(trim_long_emphasis(content), 1)
        bullet = content["sections"][0]["bullets"][0]
        # 每处强调色 span 不再超过上限
        self.assertTrue(all(len(m) <= COLOR_SPAN_MAX for m in COLOR_MARKER_RE.findall(bullet)))
        # 可见正文一字不变（解出的部分仍在）
        self.assertEqual(visible_text_length(bullet), before_visible)
        # 幂等
        self.assertEqual(trim_long_emphasis(content), 0)

    def test_ensure_length_floors_pads_short_bullet_and_alert(self):
        content = {
            "alert": "[[c:导读]]" + "读" * 170,  # 可见 172 < 180
            "sections": [
                {"id": "macro", "bullets": ["[[c:判断]]" + "论" * 94]},  # 可见 96 < 100
            ],
        }
        self.assertEqual(visible_text_length(content["alert"]), 172)
        self.assertEqual(visible_text_length(content["sections"][0]["bullets"][0]), 96)
        filled = ensure_length_floors(content)
        self.assertEqual(filled, 2)
        self.assertGreaterEqual(visible_text_length(content["alert"]), 180)
        self.assertGreaterEqual(visible_text_length(content["sections"][0]["bullets"][0]), 100)
        # 幂等：达标后不再改动
        self.assertEqual(ensure_length_floors(content), 0)

    def test_ensure_length_floors_thin_profile_floor_90(self):
        content = {"alert": "x" * 200, "sections": [
            {"id": "ai_tech", "bullets": ["[[c:判断]]" + "论" * 84]}]}  # 可见 86
        self.assertEqual(visible_text_length(content["sections"][0]["bullets"][0]), 86)
        # 淡日档下限 90 → 补足到 90+
        filled = ensure_length_floors(content, THIN_PROFILE["min_bullet_chars"])
        self.assertEqual(filled, 1)
        self.assertGreaterEqual(visible_text_length(content["sections"][0]["bullets"][0]), 90)

    def test_ensure_length_floors_skips_empty_bullet(self):
        content = {"alert": "x" * 200, "sections": [{"id": "macro", "bullets": [""]}]}
        # 空段不凭空造整段，交给结构校验拦截
        self.assertEqual(ensure_length_floors(content), 0)
        self.assertEqual(content["sections"][0]["bullets"][0], "")


class AlertCeilingTests(unittest.TestCase):
    """alert 超上限时确定性删分句（2026-10-10 事故）。"""

    def _alert(self):
        return ("**过去24小时核心变化**：" + "首" * 60 + "，[[c:布伦特收104.59美元]]；"
                + "科" * 60 + "，纳指涨0.64%；" + "白宫强制通报漏洞" + "软" * 50 + "；"
                + "黄金涨1.47%" + "金" * 40 + "。[[c:资金转向蓝筹]]。")

    def test_trims_into_range_prefers_fact_free_clause(self):
        content = {"alert": self._alert()}
        self.assertGreater(visible_text_length(content["alert"]), ALERT_MAX)
        self.assertEqual(ensure_alert_ceiling(content), 1)
        alert = content["alert"]
        self.assertTrue(ALERT_MIN <= visible_text_length(alert) <= ALERT_MAX)
        self.assertNotIn("白宫", alert)
        self.assertTrue(alert.startswith("**过去24小时核心变化**"))
        self.assertIn("[[c:资金转向蓝筹]]", alert)
        self.assertEqual(ensure_alert_ceiling(content), 0)  # 幂等

    def test_drops_fact_clauses_when_needed_and_fixes_trailing_semicolon(self):
        content = {"alert": "**过去24小时核心变化**：" + "甲" * 190 + "；" + "乙涨1%" * 20 + "；"}
        ensure_alert_ceiling(content)
        self.assertTrue(ALERT_MIN <= visible_text_length(content["alert"]) <= ALERT_MAX)
        self.assertTrue(content["alert"].endswith("。"))

    def test_never_drops_below_floor_or_first_clause(self):
        content = {"alert": "**过去24小时核心变化**：" + "甲" * 300 + "。"}
        self.assertEqual(ensure_alert_ceiling(content), 0)  # 单句无法删，交结构校验
        content = {"alert": "x" * 200}
        self.assertEqual(ensure_alert_ceiling(content), 0)

    def test_does_not_split_inside_emphasis_marker(self):
        content = {"alert": "**过去24小时核心变化**：" + "甲" * 200 + "[[c:乙；丙]]" + "丁" * 70 + "。"}
        ensure_alert_ceiling(content)
        self.assertEqual(content["alert"].count("[["), content["alert"].count("]]"))


class CompressBulletsTests(unittest.TestCase):
    """超限侧确定性压缩：删冗余衔接/解读句，保数字/强调色/首句/下限，幂等。"""

    def _doc(self):
        fact = "关键指标升至[[c:4.99%]]并被市场充分定价，成为核心变量。"
        fill = "这一趋势反映了当前市场结构性的深层变化并值得持续关注其演进。"
        head = "产业主线出现明确信号，盘面给出同向印证并形成合力。"
        tail = "后续仍需以关键数据与盘面反应作为验证条件。"
        bullet = head + fact + fact + fill + fill + fill + tail
        return {"sections": [
            {"id": "focus", "bullets": [bullet, bullet, bullet, bullet]},
            {"id": "ai_tech", "bullets": [bullet, bullet, bullet, bullet]},
            {"id": "macro", "bullets": [bullet, bullet, bullet]},
        ]}

    @staticmethod
    def _emphasis(content):
        txt = "".join(b for s in content["sections"] for b in s["bullets"])
        return re.findall(r"\[\[.+?\]\]", txt)

    @staticmethod
    def _digits(content):
        txt = "".join(b for s in content["sections"] for b in s["bullets"])
        return re.findall(r"\d", txt)

    def test_noop_when_within_range(self):
        content = self._doc()
        self.assertEqual(compress_bullets(content, 0), 0)
        self.assertEqual(compress_bullets(content, -50), 0)

    def test_cuts_at_least_requested(self):
        content = self._doc()
        before = sum(visible_text_length(b) for s in content["sections"] for b in s["bullets"])
        removed = compress_bullets(content, 200, MIN_BULLET_CHARS)
        after = sum(visible_text_length(b) for s in content["sections"] for b in s["bullets"])
        self.assertGreaterEqual(removed, 200)
        self.assertEqual(before - after, removed)

    def test_preserves_facts_and_emphasis(self):
        content = self._doc()
        emp_before, dig_before = self._emphasis(content), self._digits(content)
        compress_bullets(content, 200, MIN_BULLET_CHARS)
        # 数字与强调色 span 一字不少
        self.assertEqual(self._emphasis(content), emp_before)
        self.assertEqual(self._digits(content), dig_before)

    def test_keeps_first_sentence_and_floor(self):
        content = self._doc()
        compress_bullets(content, 300, MIN_BULLET_CHARS)
        for section in content["sections"]:
            floor = MIN_BULLET_CHARS[section["id"]]
            for bullet in section["bullets"]:
                self.assertTrue(bullet.startswith("产业主线出现明确信号"))
                self.assertGreaterEqual(visible_text_length(bullet), floor)

    def test_only_touches_bullets(self):
        content = self._doc()
        content["alert"] = "导读" + "字" * 200
        content["logic_chain"] = {"priority": "利率主线不变。这一判断值得关注。"}
        content["sections"][0]["note"] = "本节结论。这一趋势值得关注。"
        alert0, prio0, note0 = content["alert"], content["logic_chain"]["priority"], content["sections"][0]["note"]
        compress_bullets(content, 200, MIN_BULLET_CHARS)
        self.assertEqual(content["alert"], alert0)
        self.assertEqual(content["logic_chain"]["priority"], prio0)
        self.assertEqual(content["sections"][0]["note"], note0)

    def test_idempotent_second_pass_when_exhausted(self):
        content = self._doc()
        compress_bullets(content, 100000, MIN_BULLET_CHARS)  # 尽量删
        snapshot = [list(s["bullets"]) for s in content["sections"]]
        compress_bullets(content, 100000, MIN_BULLET_CHARS)  # 再删：无可删句
        self.assertEqual([list(s["bullets"]) for s in content["sections"]], snapshot)

    def test_short_bullet_untouched(self):
        content = {"sections": [{"id": "macro", "bullets": ["只有一句话的短段。"]}]}
        self.assertEqual(compress_bullets(content, 50, MIN_BULLET_CHARS), 0)


if __name__ == "__main__":
    unittest.main()
