import datetime as dt
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import fetch_news as f
import write_brief as w
import test_news_sources

class FreshnessCoverageTests(unittest.TestCase):
    def test_summary_and_url_survive_pool_and_full_pool_is_available_to_repair(self):
        helper=test_news_sources.NewsPoolTierTests()
        items=helper.news('cnbc',45)
        for item in items:
            item.update(summary='A'*1100,url='https://example.com/story')
        pool=helper.pool(items)
        self.assertEqual(len(pool[-1]['summary']),1100)
        self.assertEqual(pool[-1]['url'],'https://example.com/story')
        prompt=w.build_repair_prompt({},'feedback',pool,'20261115','date','time','asof','index','qdii')
        self.assertIn(pool[-1]['title'],prompt)
        self.assertIn('[45]',prompt)

    def test_geopolitics_is_not_starved_by_source_order(self):
        helper=test_news_sources.NewsPoolTierTests()
        items=helper.news('reuters',100)
        items+=helper.news('jin10-flash',3,prefix='伊朗霍尔木兹新通报')
        pool=helper.pool(items)
        self.assertEqual(len(pool),w.NEWS_CAP)
        self.assertEqual(sum('伊朗' in i['title'] for i in pool),3)

    def test_ranking_refresh_time_is_not_news_publication_time(self):
        with patch.object(f,'_fetch_json',return_value={'updatedTime':1794720000000,'items':[{'title':'old','url':'https://example.com'}]}):
            items=f.fetch_alphaear('cls')
        self.assertIsNone(items[0]['published_at'])
        self.assertEqual(items[0]['timestamp_basis'],'ranking_updated_at')
        self.assertTrue(items[0]['discovered_at'])

    def test_verified_supplement_uses_same_window(self):
        helper=test_news_sources.NewsPoolTierTests()
        good=helper.news('official',1)[0]
        good.update(verified=True,url='https://official.example.com/news')
        stale={**good,'title':'stale','published_at':'2026-09-25T12:00:00+08:00'}
        bad={**good,'title':'not verified','verified':False}
        with tempfile.TemporaryDirectory() as tmp,patch.object(w,'BASE',Path(tmp)):
            Path(tmp,'news_feed.json').write_text(json.dumps({'items':[]}))
            Path(tmp,'supplemental_news.json').write_text(json.dumps({'items':[good,stale,bad]}))
            pool=w.build_news_pool(helper.START,helper.END)
        self.assertEqual([i['title'] for i in pool],[good['title']])

    def test_same_subject_with_new_decision_is_retained(self):
        previous={'main_title':'','sections':[{'bullets':['英伟达宣布调整管理团队，市场关注供应链合作。']}]}
        update={'title':'英伟达宣布调整管理团队，现已宣布启动外部合规调查并聘请律师','summary':''}
        kept,dropped=w.filter_previous_events([update],previous)
        self.assertEqual(kept,[update]);self.assertEqual(dropped,[])

    def test_naive_publication_time_is_not_accepted(self):
        self.assertIsNone(w.parse_iso('2026-11-15T12:00:00'))
        self.assertIsNotNone(w.parse_iso('2026-11-15T12:00:00+08:00'))

    def test_named_beats_have_explicit_acquisition_sources(self):
        self.assertIn('cnbc-tech',f.RSS_FEEDS);self.assertIn('cnbc-world',f.RSS_FEEDS)
        self.assertTrue(any('Hormuz' in q for q in f.BING_REUTERS_QUERIES))
        self.assertTrue(any('霍尔木兹' in q for q in f.FUTU_KEYWORDS))
