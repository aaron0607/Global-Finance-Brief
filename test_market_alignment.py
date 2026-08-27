import contextlib
import datetime as dt
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import build_brief
import fetch_market


class ClosedIndexAlignmentTests(unittest.TestCase):
    def rows(self, price_date="2026-08-07", vix_date="2026-08-07"):
        rows = {
            symbol: {"session_date": price_date}
            for symbol in fetch_market.PRICE_INDEX_SYMS
        }
        rows[fetch_market.VIX_SYMBOL] = {"session_date": vix_date}
        return rows

    def test_same_day_is_valid(self):
        result = fetch_market.validate_closed_index_dates(self.rows())
        self.assertTrue(result["valid"])
        self.assertFalse(result["misaligned"])
        self.assertEqual(result["vix_lag_calendar_days"], 0)
        self.assertEqual(result["index_date_label"], "8月7日收盘")

    def test_one_day_vix_publication_lag_is_valid_and_labeled(self):
        result = fetch_market.validate_closed_index_dates(
            self.rows(vix_date="2026-08-06")
        )
        self.assertTrue(result["valid"])
        self.assertFalse(result["misaligned"])
        self.assertEqual(result["vix_lag_calendar_days"], 1)
        self.assertEqual(result["index_date_label"], "8月7日收盘（VIX截至8月6日）")

    def test_price_indices_must_still_align(self):
        rows = self.rows(vix_date="2026-08-06")
        rows["^SOX"]["session_date"] = "2026-08-06"
        result = fetch_market.validate_closed_index_dates(rows)
        self.assertFalse(result["valid"])
        self.assertTrue(result["misaligned"])
        self.assertIn("四个美股价格指数", result["errors"][0]["error"])

    def test_vix_lag_beyond_limit_is_invalid(self):
        result = fetch_market.validate_closed_index_dates(
            self.rows(vix_date="2026-08-03")
        )
        self.assertFalse(result["valid"])
        self.assertIn("超过允许", result["errors"][0]["error"])

    def test_vix_cannot_be_later_than_price_indices(self):
        result = fetch_market.validate_closed_index_dates(
            self.rows(price_date="2026-08-06", vix_date="2026-08-07")
        )
        self.assertFalse(result["valid"])
        self.assertIn("晚于", result["errors"][0]["error"])


class BuildSnapshotAuditTests(unittest.TestCase):
    def test_public_data_source_label_matches_current_providers(self):
        self.assertNotIn("Yahoo Finance", build_brief.PUBLIC_DATA_SOURCES)
        for provider in ("天天基金", "新浪财经", "东方财富", "CBOE"):
            self.assertIn(provider, build_brief.PUBLIC_DATA_SOURCES)
        self.assertNotIn("Yahoo Finance", Path(build_brief.__file__).read_text(encoding="utf-8"))

    def snapshot(self):
        indices = []
        for symbol in fetch_market.PRICE_INDEX_SYMS:
            indices.append({
                "symbol": symbol,
                "session_date": "2026-08-07",
                "quote_time": "2026-08-08T05:00:00+08:00",
                "market_state": "休市",
                "provider": "test",
                "change_basis": "较前收盘",
            })
        indices.append({
            "symbol": fetch_market.VIX_SYMBOL,
            "session_date": "2026-08-06",
            "quote_time": "2026-08-07T04:00:00+08:00",
            "market_state": "休市",
            "provider": "test",
            "change_basis": "较前收盘",
        })
        return {
            "schema_version": 2,
            "valid": True,
            "run_started_at": "2026-08-08T08:20:00+08:00",
            "fetched_at": dt.datetime.now(build_brief.BRIEF_TZ).isoformat(timespec="seconds"),
            "errors": [],
            "misaligned": False,
            "price_index_session_date": "2026-08-07",
            "vix_session_date": "2026-08-06",
            "vix_lag_calendar_days": 1,
            "vix_max_lag_calendar_days": 3,
            "index_date_label": "8月7日收盘（VIX截至8月6日）",
            "index_price_label": "收盘价",
            "commodity_quote_date": "2026-08-08",
            "commodity_date_label": "8月8日",
            "indices": indices,
            "commodities": [],
        }

    def fill_market(self, snapshot):
        content = {"date_yyyymmdd": "20260808"}
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "market_snapshot.json"
            path.write_text(json.dumps(snapshot), encoding="utf-8")
            with mock.patch.object(build_brief, "MARKET_SNAPSHOT", path):
                return build_brief.fill_market(content)

    def test_builder_accepts_audited_vix_lag(self):
        self.assertIn("行情快照", self.fill_market(self.snapshot()))

    def test_commodity_header_uses_actual_quote_date(self):
        html = build_brief.render_commodity_table("8月8日", [])
        self.assertIn("大宗 / 外汇（8月8日）", html)
        self.assertNotIn("最新数据", html)

    def test_builder_rejects_tampered_vix_lag(self):
        snapshot = self.snapshot()
        snapshot["vix_lag_calendar_days"] = 0
        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as raised:
                self.fill_market(snapshot)
        self.assertIn("滞后元数据", str(raised.exception))

    def test_builder_rejects_lag_beyond_declared_limit(self):
        snapshot = self.snapshot()
        snapshot["indices"][4]["session_date"] = "2026-08-03"
        snapshot["vix_session_date"] = "2026-08-03"
        snapshot["vix_lag_calendar_days"] = 4
        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as raised:
                self.fill_market(snapshot)
        self.assertIn("不合规", str(raised.exception))


class FinalRenderQATests(unittest.TestCase):
    def test_valid_dates_and_weekdays_pass(self):
        content = {"date_yyyymmdd": "20260814", "date_cn": "2026年8月14日（周五）"}
        html = "<p>2026年8月14日（周五） · 过去24小时财经简报</p><p>估算基于2026年8月13日（周四）</p>"
        self.assertIn("日期/星期一致", build_brief.validate_final_render(content, html))

    def test_empty_weekday_is_rejected(self):
        content = {"date_yyyymmdd": "20260814", "date_cn": "2026年8月14日（周）"}
        html = "<p>2026年8月14日（周） · 过去24小时财经简报</p>"
        with self.assertRaises(SystemExit) as raised:
            build_brief.validate_final_render(content, html)
        self.assertIn("星期内容为空", str(raised.exception))

    def test_wrong_secondary_weekday_is_rejected(self):
        content = {"date_yyyymmdd": "20260814", "date_cn": "2026年8月14日（周五）"}
        html = "<p>2026年8月14日（周五）</p><p>2026年8月13日（周二）</p>"
        with self.assertRaises(SystemExit) as raised:
            build_brief.validate_final_render(content, html)
        self.assertIn("8月13日应为周四", str(raised.exception))

    def test_template_placeholder_is_rejected(self):
        content = {"date_yyyymmdd": "20260814", "date_cn": "2026年8月14日（周五）"}
        html = "<p>2026年8月14日（周五）</p><p>8月X日 周X</p>"
        with self.assertRaises(SystemExit) as raised:
            build_brief.validate_final_render(content, html)
        self.assertIn("模板占位符", str(raised.exception))

    def test_rendered_program_structure_is_rejected(self):
        content = {"date_yyyymmdd": "20260814", "date_cn": "2026年8月14日（周五）"}
        html = "<p>2026年8月14日（周五）</p><p>{'text': '正文', 'emphasis': '重点'}</p>"
        with self.assertRaises(SystemExit) as raised:
            build_brief.validate_final_render(content, html)
        self.assertIn("程序结构残留", str(raised.exception))

    def test_rendered_object_with_other_field_names_is_rejected(self):
        content = {"date_yyyymmdd": "20260814", "date_cn": "2026年8月14日（周五）"}
        html = '<p>2026年8月14日（周五）</p><p>{"body": "正文", "highlights": ["重点"]}</p>'
        with self.assertRaises(SystemExit) as raised:
            build_brief.validate_final_render(content, html)
        self.assertIn("Python/JSON 程序结构残留", str(raised.exception))


if __name__ == "__main__":
    unittest.main()
