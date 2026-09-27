"""新闻源：Reuters 抓取解析与新闻池海外优先分级。"""

import datetime as dt
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import fetch_news
import write_brief

BJT = write_brief.BRIEF_TZ


def sitemap_url(loc, published, title):
    return (
        "<url><loc>%s</loc><news:news><news:publication><news:name>Reuters</news:name>"
        "</news:publication><news:publication_date>%s</news:publication_date>"
        "<news:title><![CDATA[%s]]></news:title></news:news>"
        "<image:image><image:loc>https://www.reuters.com/resizer/x.jpg</image:loc></image:image></url>"
        % (loc, published, title)
    )


def sitemap(*urls):
    return '<?xml version="1.0"?><urlset>%s</urlset>' % "".join(urls)


class ReutersSitemapTests(unittest.TestCase):
    NOW = dt.datetime(2026, 9, 27, 1, 0, tzinfo=dt.timezone.utc)

    def test_parses_whitelisted_sections_with_exact_time(self):
        rows = fetch_news.parse_reuters_sitemap(sitemap(
            sitemap_url("https://www.reuters.com/markets/us/stocks-2026-09-26/",
                        "2026-09-26T20:00:00.5Z", "Wall St ends higher &amp; Nvidia gains"),
            sitemap_url("https://www.reuters.com/sports/soccer/x-2026-09-26/",
                        "2026-09-26T19:00:00Z", "Soccer"),
            sitemap_url("https://www.reuters.com/business/environment/storm-2026-09-26/",
                        "2026-09-26T18:00:00Z", "Storm"),
            sitemap_url("https://www.reuters.com/fr/affaires/x-2026-09-26/",
                        "2026-09-26T17:00:00Z", "Bourse"),
        ))
        self.assertEqual(len(rows), 4)
        kept = [item for _, item in rows if item]
        self.assertEqual(len(kept), 1)
        self.assertEqual(kept[0]["source_id"], "reuters")
        self.assertEqual(kept[0]["title"], "Wall St ends higher & Nvidia gains")
        self.assertEqual(kept[0]["published_at"], "2026-09-27T04:00:00.500000+08:00")

    def test_paginates_until_cutoff(self):
        page0 = sitemap(sitemap_url("https://www.reuters.com/business/a-2026-09-26/",
                                    "2026-09-26T23:00:00Z", "A"))
        page1 = sitemap(
            sitemap_url("https://www.reuters.com/technology/b-2026-09-26/", "2026-09-26T10:00:00Z", "B"),
            sitemap_url("https://www.reuters.com/business/c-2026-09-24/", "2026-09-24T10:00:00Z", "Too old"),
        )
        pages = [page0, page1, sitemap()]
        responses = []
        for text in pages:
            resp = mock.Mock(text=text)
            resp.raise_for_status = mock.Mock()
            responses.append(resp)
        with mock.patch.object(fetch_news.SESSION, "get", side_effect=responses) as get:
            items = fetch_news.fetch_reuters_sitemap(self.NOW)
        self.assertEqual(get.call_count, 2)  # 第二页已越过 26h 截止点，不再翻第三页
        self.assertEqual([it["title"] for it in items], ["A", "B"])

    def test_sitemap_failure_falls_back_to_bing(self):
        with mock.patch.object(fetch_news, "fetch_reuters_sitemap", side_effect=OSError("blocked")), \
                mock.patch.object(fetch_news, "fetch_reuters_bing", return_value=[{"title": "bing"}]) as bing, \
                mock.patch("sys.stderr"):
            self.assertEqual(fetch_news.fetch_reuters(), [{"title": "bing"}])
        bing.assert_called_once()


class HtmlEntityTests(unittest.TestCase):
    def test_rss_entities_are_decoded(self):
        items = fetch_news._parse_rss(
            "<item><title>Nor&apos;easter &amp; Boeing&#x27;s glitch</title>"
            "<pubDate>Sat, 26 Sep 2026 22:23:00 GMT</pubDate></item>")
        self.assertEqual(items[0]["title"], "Nor'easter & Boeing's glitch")


class ReutersBingTests(unittest.TestCase):
    @staticmethod
    def item(url, pub, source="Reuters", title="T"):
        link = ("http://www.bing.com/news/apiclick.aspx?ref=FexRss&amp;url=%s&amp;mkt=en-us"
                % url.replace(":", "%3a").replace("/", "%2f"))
        return ("<item><title>%s</title><link>%s</link><description>D</description>"
                "<pubDate>%s</pubDate><News:Source>%s</News:Source></item>" % (title, link, pub, source))

    def test_keeps_only_reuters_articles_with_consistent_dates(self):
        xml = "<rss><channel>%s</channel></rss>" % "".join([
            self.item("https://www.reuters.com/markets/ok-2026-09-25/", "Fri, 25 Sep 2026 12:00:00 GMT", title="OK"),
            self.item("https://www.reuters.com/markets/stale-2026-09-15/", "Fri, 25 Sep 2026 12:00:00 GMT"),
            self.item("https://www.reuters.com/markets/other-2026-09-25/", "Fri, 25 Sep 2026 12:00:00 GMT", source="Yahoo"),
            self.item("https://www.reuters.com/markets/quote/CNX.DG", "Fri, 25 Sep 2026 12:00:00 GMT"),
        ])
        items = fetch_news.parse_bing_reuters(xml)
        self.assertEqual([it["title"] for it in items], ["OK"])
        self.assertEqual(items[0]["url"], "https://www.reuters.com/markets/ok-2026-09-25/")
        self.assertEqual(items[0]["published_at"], "2026-09-25T20:00:00+08:00")


class NewsPoolTierTests(unittest.TestCase):
    START = dt.datetime(2026, 9, 26, 8, 0, tzinfo=BJT)
    END = dt.datetime(2026, 9, 27, 8, 0, tzinfo=BJT)

    def pool(self, items):
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, "news_feed.json").write_text(json.dumps({"items": items}), encoding="utf-8")
            with mock.patch.object(write_brief, "BASE", Path(tmp)):
                return write_brief.build_news_pool(self.START, self.END)

    @staticmethod
    def news(source_id, n, hour=12, prefix=None):
        return [{
            "source_id": source_id, "source_name": source_id,
            "title": "%s-%d" % (prefix or source_id, i),
            "published_at": "2026-09-26T%02d:%02d:00+08:00" % (hour, i % 60),
        } for i in range(n)]

    def test_overseas_first_then_domestic_by_recency(self):
        items = (self.news("xueqiu", 2, hour=20) + self.news("wscn-live", 2, hour=18)
                 + self.news("reuters", 2, hour=10) + self.news("bloomberg", 1, hour=11))
        pool = self.pool(items)
        self.assertEqual([it["title"] for it in pool],
                         ["bloomberg-0", "reuters-1", "reuters-0",
                          "wscn-live-1", "wscn-live-0", "xueqiu-1", "xueqiu-0"])

    def test_overseas_capped_to_keep_domestic_share(self):
        items = self.news("reuters", 50) + self.news("cnbc", 40) + self.news("cls", 60)
        pool = self.pool(items)
        self.assertEqual(len(pool), write_brief.NEWS_CAP)
        self.assertEqual(sum(1 for it in pool if it["tier"] == 1), write_brief.OVERSEAS_CAP)
        self.assertEqual(sum(1 for it in pool if it["source_id"] == "cls"), 40)

    def test_leftover_overseas_backfills_when_domestic_is_thin(self):
        items = self.news("reuters", 50) + self.news("cnbc", 40) + self.news("jin10-flash", 5)
        pool = self.pool(items)
        self.assertEqual(len(pool), 95)
        self.assertEqual(sum(1 for it in pool if it["tier"] == 1), 90)

    def test_out_of_window_and_duplicate_titles_dropped(self):
        items = [
            {"source_id": "cls", "title": "同一标题", "published_at": "2026-09-26T12:00:00+08:00"},
            {"source_id": "reuters", "title": "同一标题", "published_at": "2026-09-26T11:00:00+08:00"},
            {"source_id": "cnbc", "title": "old", "published_at": "2026-09-25T12:00:00+08:00"},
            {"source_id": "cnbc", "title": "no time"},
        ]
        pool = self.pool(items)
        self.assertEqual([(it["source_id"], it["title"]) for it in pool], [("reuters", "同一标题")])

    def test_unknown_source_is_lowest_tier(self):
        self.assertEqual(write_brief.source_tier("something-new"), 3)
        self.assertEqual(write_brief.source_tier("reuters"), 1)


if __name__ == "__main__":
    unittest.main()
