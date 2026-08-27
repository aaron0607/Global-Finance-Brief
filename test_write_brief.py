import unittest
import datetime as dt
import re
from unittest.mock import patch

from write_brief import (
    WEEKDAYS,
    build_repair_prompt,
    ensure_main_title,
    ensure_note_floor,
    fallback_main_title,
    normalize_main_title,
    sanitize_content,
    title_news_errors,
    title_ok,
    validate_required,
    visible_text_length,
)
from content_validation import COLOR_MARKER_RE, ensure_emphasis, validate_no_structure_leaks


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


if __name__ == "__main__":
    unittest.main()
