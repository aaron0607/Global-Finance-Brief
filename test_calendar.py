"""本周财经日历：采集筛选与硬校验回归测试。

背景：2026-08-29/08-30 成稿日历为空，08-31 把 9 月 4 日的非农写成当天。
日历改由 fetch_calendar.py 从结构化日历源确定性生成后，这里锁住筛选口径与拦截规则。
"""

import datetime as dt
import json
import unittest
from unittest.mock import patch
from zoneinfo import ZoneInfo

import fetch_calendar
from content_validation import validate_calendar
from write_brief import apply_calendar_feed

TZ = ZoneInfo("Asia/Shanghai")

JIN10_SAMPLE = {
    "status": 200,
    "data": [
        # 已过去的条目：本次窗口起点之前，必须剔除
        {"title": "日本7月零售销售年率", "pub_time": "2026-08-31 07:50", "star": 2},
        {"title": "中国8月官方制造业PMI", "pub_time": "2026-08-31 09:30", "star": 3,
         "consensus": "49.6", "previous": "49.2"},
        # 管道类噪音：国债竞拍 / EIA 分项 / ISM 分项
        {"title": "美国至9月1日1年期国债竞拍-得标利率", "pub_time": "2026-09-01 23:30", "star": 2},
        {"title": "美国8月ISM就业指数", "pub_time": "2026-09-01 22:00", "star": 2},
        {"title": "美国至8月28日当周API原油库存(万桶)", "pub_time": "2026-09-02 04:30", "star": 3},
        # 应当入选的一线数据
        {"title": "美国8月ISM制造业PMI", "pub_time": "2026-09-01 22:00", "star": 4,
         "consensus": "55.2", "previous": "55.6"},
        {"title": "美国8月ADP就业人数(万人)", "pub_time": "2026-09-02 20:15", "star": 4},
        {"title": "美国至8月29日当周初请失业金人数(万人)", "pub_time": "2026-09-03 20:30", "star": 4},
        # 同一时刻的一组就业数据：应合并成一行
        {"title": "美国8月季调后非农就业人口(万人)", "pub_time": "2026-09-04 20:30", "star": 5},
        {"title": "美国8月失业率", "pub_time": "2026-09-04 20:30", "star": 5},
        {"title": "美国8月平均每小时工资月率", "pub_time": "2026-09-04 20:30", "star": 3},
    ],
}


def build_from_sample(now, days=7, max_count=5, min_count=3, eastmoney=None):
    with patch.object(fetch_calendar, "JIN10_MCP_TOKEN", "test-token"), \
         patch.object(fetch_calendar, "jin10_call", lambda *a, **k: JIN10_SAMPLE), \
         patch.object(fetch_calendar, "fetch_eastmoney_calendar",
                      lambda *a, **k: (eastmoney or [], [])):
        return fetch_calendar.build_feed(days, max_count, min_count, now=now)


class FeedSelectionTests(unittest.TestCase):
    def setUp(self):
        self.now = dt.datetime(2026, 8, 31, 8, 20, tzinfo=TZ)
        self.feed = build_from_sample(self.now)

    def test_feed_is_not_empty(self):
        self.assertTrue(self.feed["events"], "确定性日历不应产出空表")

    def test_drops_past_events(self):
        titles = " ".join(e["event"] for e in self.feed["events"])
        self.assertNotIn("零售销售", titles)

    def test_drops_plumbing_noise(self):
        titles = " ".join(e["event"] for e in self.feed["events"])
        for noise in ("竞拍", "ISM就业指数", "API"):
            self.assertNotIn(noise, titles)

    def test_merges_same_timestamp_group(self):
        payroll = [e for e in self.feed["events"] if "非农" in e["event"]]
        self.assertEqual(len(payroll), 1, "同一时刻的就业数据应合并成一行")
        self.assertIn("失业率", payroll[0]["event"])
        self.assertIn("非农", payroll[0]["event"].split("、")[0])
        self.assertTrue(payroll[0]["event"].startswith("20:30"))

    def test_payroll_date_and_weekday(self):
        payroll = next(e for e in self.feed["events"] if "非农" in e["event"])
        self.assertEqual(payroll["date_iso"], "2026-09-04")
        self.assertEqual(payroll["date"], "9月4日 周五")
        self.assertEqual(payroll["stars"], 3)

    def test_events_sorted_and_capped(self):
        dates = [e["date_iso"] for e in self.feed["events"]]
        self.assertEqual(dates, sorted(dates))
        self.assertLessEqual(len(self.feed["events"]), 5)

    def test_window_limits_lookahead(self):
        feed = build_from_sample(self.now, days=2)
        self.assertTrue(all(e["date_iso"] <= "2026-09-02" for e in feed["events"]))

    def test_feed_matches_validator_contract(self):
        content = {"date_yyyymmdd": "20260901", "calendar": []}
        apply_calendar_feed(content, build_from_sample(
            dt.datetime(2026, 9, 1, 8, 20, tzinfo=TZ))["events"])
        self.assertTrue(content["calendar"])
        self.assertEqual(validate_calendar(content), [])


class EastmoneyNormalizationTests(unittest.TestCase):
    def test_period_extraction(self):
        self.assertEqual(fetch_calendar._eastmoney_period("美国:非农就业人数(报告期:2026年08月)"), "8月")

    def test_event_title_drops_duplicate_date(self):
        self.assertEqual(
            fetch_calendar.clean_title("特斯拉将于9月3日在美国举行Cybercab发布会"),
            "特斯拉将在美国举行Cybercab发布会",
        )


class PeriodMajorityTests(unittest.TestCase):
    """东方财富偶发把同场数据的报告期写错（9/10 的 PPI 混入一条 09 月，其余为 08 月）。"""

    def test_modal_period_wins(self):
        when = dt.datetime(2026, 9, 10, 20, 30, tzinfo=TZ)
        items = [
            {"when": when, "title": "美国9月PPI", "region": "美国", "tag": "PPI", "importance": 4,
             "score": 9, "kind": "data", "source": "eastmoney-fecalendar", "period": "9月"},
            {"when": when, "title": "美国8月PPI", "region": "美国", "tag": "PPI", "importance": 4,
             "score": 9, "kind": "data", "source": "eastmoney-fecalendar", "period": "8月"},
            {"when": when, "title": "美国8月PPI同比", "region": "美国", "tag": "PPI", "importance": 4,
             "score": 9, "kind": "data", "source": "eastmoney-fecalendar", "period": "8月"},
        ]
        kept = fetch_calendar.dedupe(items)
        self.assertEqual(len(kept), 1)
        self.assertTrue(kept[0]["title"].startswith("美国8月"))


class CalendarValidationTests(unittest.TestCase):
    def content(self, calendar, day="20260901"):
        return {"date_yyyymmdd": day, "calendar": calendar}

    def test_empty_calendar_is_rejected(self):
        errors = validate_calendar(self.content([]))
        self.assertTrue(any("为空" in e for e in errors))

    def test_wrong_weekday_is_rejected(self):
        errors = validate_calendar(self.content(
            [{"date": "9月4日 周一", "date_iso": "2026-09-04", "event": "非农", "stars": 3}]))
        self.assertTrue(any("星期错配" in e for e in errors))

    def test_stale_date_is_rejected(self):
        errors = validate_calendar(self.content(
            [{"date": "8月31日 周一", "date_iso": "2026-08-31", "event": "非农", "stars": 3}]))
        self.assertTrue(any("早于简报日期" in e for e in errors))

    def test_far_future_is_rejected(self):
        errors = validate_calendar(self.content(
            [{"date": "10月1日 周四", "date_iso": "2026-10-01", "event": "非农", "stars": 3}]))
        self.assertTrue(any("展望窗口" in e for e in errors))

    def test_unsorted_rows_are_rejected(self):
        errors = validate_calendar(self.content([
            {"date": "9月4日 周五", "date_iso": "2026-09-04", "event": "非农", "stars": 3},
            {"date": "9月2日 周三", "date_iso": "2026-09-02", "event": "ADP", "stars": 2},
        ]))
        self.assertTrue(any("升序" in e for e in errors))

    def test_free_form_date_is_rejected(self):
        errors = validate_calendar(self.content(
            [{"date": "2026-09-04", "event": "非农", "stars": 3}]))
        self.assertTrue(any("9月4日 周五" in e for e in errors))

    def test_year_rollover_without_iso(self):
        errors = validate_calendar(self.content(
            [{"date": "1月2日 周六", "event": "非农", "stars": 3}], day="20261231"))
        self.assertEqual(errors, [])

    def test_historical_content_is_not_retroactively_checked(self):
        self.assertEqual(validate_calendar(self.content([], day="20260820")), [])

    def test_valid_calendar_passes(self):
        errors = validate_calendar(self.content([
            {"date": "9月2日 周三", "date_iso": "2026-09-02", "event": "20:15 美国8月ADP就业人数", "stars": 2},
            {"date": "9月4日 周五", "date_iso": "2026-09-04", "event": "20:30 美国8月非农就业人口、失业率", "stars": 3},
        ]))
        self.assertEqual(errors, [])


class FeedLoadingTests(unittest.TestCase):
    def test_stale_feed_is_refused(self):
        import write_brief
        payload = {
            "schema_version": 1,
            "fetched_at": (dt.datetime.now(TZ) - dt.timedelta(hours=30)).isoformat(),
            "events": [{"date": "9月4日 周五", "date_iso": "2026-09-04", "event": "非农", "stars": 3}],
        }
        path = write_brief.BASE / "calendar_feed.json"
        backup = path.read_text(encoding="utf-8") if path.exists() else None
        try:
            path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            events, warnings = write_brief.load_calendar_feed()
            self.assertEqual(events, [])
            self.assertTrue(any("过期" in w for w in warnings))
        finally:
            if backup is None:
                path.unlink(missing_ok=True)
            else:
                path.write_text(backup, encoding="utf-8")


if __name__ == "__main__":
    unittest.main()


class Jin10ClientTests(unittest.TestCase):
    """金十 MCP 是无会话实现：不返回 Mcp-Session-Id，客户端不得因此判定失败。

    旧版要求该响应头，服务器上金十源长期静默为 0 条（新闻与日历都受影响）。
    """

    def test_call_succeeds_without_session_header(self):
        import fetch_news

        class FakeResponse:
            def __init__(self, body=b"", status=200):
                self.status_code = status
                self.headers = {}
                self.content = body

        calls = []

        def fake_post(url, headers=None, timeout=None, json=None):
            calls.append(json.get("method"))
            if json.get("method") == "tools/call":
                payload = {"jsonrpc": "2.0", "id": 2,
                           "result": {"content": [{"type": "text", "text": '{"status": 200, "data": []}'}]}}
                return FakeResponse(("data: " + __import__("json").dumps(payload)).encode("utf-8"))
            return FakeResponse()

        with patch.object(fetch_news, "JIN10_MCP_TOKEN", "test-token"), \
             patch.object(fetch_news.SESSION, "post", fake_post):
            result = fetch_news.jin10_call("list_calendar", {})
        self.assertEqual(result, {"status": 200, "data": []})
        self.assertIn("tools/call", calls)
