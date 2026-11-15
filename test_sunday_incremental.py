import copy
import datetime as dt
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import write_brief as w
from content_validation import SUNDAY_MODE, NONTRADING_MODE, word_limits


class SundayIncrementalTests(unittest.TestCase):
    def setUp(self):
        self.previous = {
            'date_yyyymmdd': '20261114',
            'main_title': '英伟达换帅·费半重挫3.43%',
            'sections': [{'id': 'ai_tech', 'bullets': [
                'OpenAI推出ChatGPT Wallet，允许AI代理购买商品，支持信用卡支付。',
                '英伟达宣布新订单金额达到100亿美元，订单将在明年交付。']}],
        }

    def test_sunday_is_short_even_with_normal_friday_session(self):
        snap = {'price_index_session_date': '2026-11-13'}
        self.assertIs(w.select_profile(snap, dt.date(2026, 11, 15)), w.SUNDAY_PROFILE)
        self.assertIs(w.select_profile(snap, dt.date(2026, 11, 14)), w.NORMAL_PROFILE)

    def test_short_word_limits_require_sunday_and_explicit_edition(self):
        self.assertEqual(word_limits({'date_yyyymmdd': '20261115', 'edition': SUNDAY_MODE}), (4500, 6000))
        self.assertEqual(word_limits({'date_yyyymmdd': '20261115'}), (5000, 7000))
        with self.assertRaises(ValueError):
            word_limits({'date_yyyymmdd': '20261116', 'edition': SUNDAY_MODE})

    def test_same_product_in_chinese_is_filtered_even_with_new_timestamp(self):
        item = {'title': 'OpenAI推出ChatGPT钱包，让AI代理无需加密货币即可购物', 'summary': '',
                'published_at': '2026-11-15T07:00:00+08:00'}
        kept, dropped = w.filter_previous_events([item], self.previous)
        self.assertEqual(kept, [])
        self.assertEqual(dropped, [item['title']])

    def test_follow_up_with_new_order_amount_is_retained(self):
        title = '英伟达宣布新订单金额达到120亿美元，订单将在明年交付。'
        self.assertIsNone(w.repeated_event(title, w.previous_sentences(self.previous)))

    def test_same_company_different_event_is_not_filtered(self):
        title = '英伟达宣布发布全新推理架构，采用不同互连方案。'
        self.assertIsNone(w.repeated_event(title, w.previous_sentences(self.previous)))

    def test_yesterdays_market_move_cannot_be_sunday_headline(self):
        content = {'main_title': '费半重挫3.43%·全新产业动态', 'sections': []}
        self.assertTrue(any('标题重复' in e for e in w.previous_issue_errors(content, self.previous)))

    def test_two_repeated_events_are_blocked_even_if_reordered(self):
        content = {'main_title': '昆仑芯新品明年推出', 'sections': [{'id': 'focus', 'bullets': [
            '英伟达宣布新订单金额达到100亿美元，订单将在明年交付。'
            'OpenAI推出ChatGPT钱包，允许AI代理购买商品。']} ]}
        self.assertTrue(any('正文重复' in e for e in w.previous_issue_errors(content, self.previous)))

    def test_previous_file_date_must_match_yesterday(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(w, 'BASE', Path(tmp)):
            path = Path(tmp)/'content_20261114.json'
            path.write_text(json.dumps(self.previous))
            self.assertEqual(w.load_previous_issue('20261115')['date_yyyymmdd'], '20261114')
            wrong = copy.deepcopy(self.previous); wrong['date_yyyymmdd'] = '20261113'
            path.write_text(json.dumps(wrong))
            self.assertIsNone(w.load_previous_issue('20261115'))

    def test_sunday_may_omit_sections_without_new_events(self):
        from test_write_brief import ContentRepairTests
        content = ContentRepairTests()._valid_content()
        content.update(date_yyyymmdd='20260830', edition=SUNDAY_MODE)
        content['sections'] = [content['sections'][0]]
        content['sections'][0]['bullets'] = content['sections'][0]['bullets'][:2]
        errors = w.validate_required(content, '20260830', w.SUNDAY_PROFILE['bullet_rules'], w.SUNDAY_PROFILE['min_bullet_chars'])
        self.assertEqual(errors, [])
        content.pop('edition')
        self.assertTrue(w.validate_required(content, '20260830'))

    def test_sunday_cannot_omit_every_section_or_use_unknown_section(self):
        from test_write_brief import ContentRepairTests
        content = ContentRepairTests()._valid_content()
        content.update(date_yyyymmdd='20260830', edition=SUNDAY_MODE)
        content['sections'] = []
        self.assertTrue(w.validate_required(content, '20260830', w.SUNDAY_PROFILE['bullet_rules']))
        content['sections'] = [{'id':'unknown','title':'t','bullets':['text'],'note':'n'*50}]
        self.assertTrue(w.validate_required(content, '20260830', w.SUNDAY_PROFILE['bullet_rules']))

    def test_generation_and_repair_prompts_include_prior_issue_as_exclusions(self):
        profile = {**w.SUNDAY_PROFILE, 'previous_issue': self.previous}
        snap = {'news_window': {'start': '2026-11-14T08:20:01+08:00', 'end': '2026-11-15T08:20:01+08:00'},
                'indices': [], 'commodities': [], 'extra_quotes': []}
        p = w.build_prompt(snap, [], '20261115', 'date', '08:20', 'asof', 'index', 'qdii', '', profile=profile)
        repair = w.build_repair_prompt({}, 'feedback', [], '20261115', 'date', '08:20', 'asof', 'index', 'qdii', profile)
        for prompt in (p, repair):
            self.assertIn('休市次日增量版', prompt)
            self.assertIn('昨日已覆盖事件', prompt)
            self.assertIn('5000-5600', prompt)
            self.assertIn('不是本期可引用素材', prompt)
            self.assertNotIn('focus 3 条', prompt)
            self.assertNotIn('focus 每段 260-340', prompt)

class SundayMainFlowTests(unittest.TestCase):
    def test_sunday_accepts_short_draft_with_reduced_sections(self):
        from contextlib import ExitStack
        from test_write_brief import ContentRepairTests
        content = ContentRepairTests()._valid_content()
        content.update(date_yyyymmdd='20260830', main_title='新芯片发布推动推理部署')
        for sec in content['sections']:
            sec['bullets'] = sec['bullets'][:w.SUNDAY_PROFILE['bullet_rules'][sec['id']][0]]
        snap = {'run_started_at': '2026-08-30T08:20:01+08:00',
                'price_index_session_date': '2026-08-28',
                'news_window': {'start': '2026-08-29T08:20:01+08:00', 'end': '2026-08-30T08:20:01+08:00'}}
        previous = {'date_yyyymmdd':'20260829', 'main_title':'昨日主题', 'sections':[]}
        with ExitStack() as stack:
            for name, result in (('load_snapshot', snap), ('build_news_pool', []),
                                 ('load_previous_issue', previous), ('load_calendar_feed', ([{}], [])),
                                 ('post_process', []), ('ensure_main_title', ''), ('focus_overlap_errors', []),
                                 ('run_build', (0, '正文字符数(去标签): 5300'))):
                stack.enter_context(patch.object(w, name, return_value=result))
            stack.enter_context(patch.object(w.sys, 'argv', ['write_brief.py']))
            stack.enter_context(patch.object(w.Path, 'write_text'))
            write = stack.enter_context(patch.object(w, 'write_content'))
            api = stack.enter_context(patch.object(w, 'call_deepseek', return_value=(json.dumps(content), 'stop')))
            self.assertEqual(w.main(), 0)
            self.assertEqual(api.call_count, 1)
            self.assertEqual(write.call_args.args[1]['edition'], NONTRADING_MODE)


if __name__ == '__main__':
    unittest.main()
