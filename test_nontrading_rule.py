import datetime as dt
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import build_brief as b
import write_brief as w
import content_validation as v
import test_market_alignment as fixtures

class NonTradingRuleTests(unittest.TestCase):
    def test_real_session_controls_sunday_monday_and_holiday_next_day(self):
        for date,session in [('20261115','2026-11-13'),('20261116','2026-11-13'),
                             ('20260908','2026-09-04'),('20261226','2026-12-24')]:
            with self.subTest(day=date):
                day=dt.datetime.strptime(date,'%Y%m%d').date()
                profile=w.select_profile({'price_index_session_date':session},day)
                self.assertEqual((profile['hard_min'],profile['hard_max']),(4500,6000))
                self.assertEqual(profile['edition'],v.NONTRADING_MODE)
                self.assertEqual(v.word_limits({'date_yyyymmdd':date,'edition':v.NONTRADING_MODE,
                                              'market_session_date':session}),(4500,6000))

    def test_normal_saturday_and_tuesday_use_regular_profile(self):
        for date,session in [('20261114','2026-11-13'),('20261117','2026-11-16')]:
            day=dt.datetime.strptime(date,'%Y%m%d').date()
            self.assertIs(w.select_profile({'price_index_session_date':session},day),w.NORMAL_PROFILE)

    def test_missing_bad_or_future_session_does_not_relax_word_limits(self):
        for session in [None,'bad','2026-11-16','2026-11-17']:
            with self.subTest(session=session):
                self.assertFalse(v.follows_nontrading_day('20261116',session))
                with self.assertRaises(ValueError):
                    v.word_limits({'date_yyyymmdd':'20261116','edition':v.NONTRADING_MODE,
                                   'market_session_date':session})

    def test_no_weekday_only_shortcut_when_snapshot_is_missing(self):
        self.assertIs(w.select_profile({},dt.date(2026,11,15)),w.NORMAL_PROFILE)

    def test_legacy_sunday_issue_still_validates_without_rewriting(self):
        self.assertEqual(v.word_limits({'date_yyyymmdd':'20261115','edition':v.SUNDAY_MODE}),(4500,6000))

    def test_builder_binds_edition_metadata_to_actual_snapshot(self):
        snap=fixtures.BuildSnapshotAuditTests().snapshot()
        snap['run_started_at']='2026-08-10T08:20:00+08:00'
        c={'date_yyyymmdd':'20260810','as_of':snap['run_started_at'],
           'edition':v.NONTRADING_MODE,'market_session_date':'2026-08-07'}
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'snapshot.json';path.write_text(json.dumps(snap))
            with patch.object(b,'MARKET_SNAPSHOT',path):
                self.assertIn('行情快照',b.fill_market(dict(c)))
                with self.assertRaises(SystemExit):
                    b.fill_market({**c,'market_session_date':'2026-08-06'})
