#!/usr/bin/env python3
"""生成云端写稿用的新闻池 news_pool.json：本期 24 小时窗口内、去重、按来源分级排序（海外一手优先）。

与 DeepSeek 写稿器（write_brief.build_news_pool）同一口径，保证两条写稿通道看到的新闻池一致；
窗口取 market_snapshot.json 的 news_window（即 [as_of-24h, as_of]），池外新闻不得进入正文。

用法：.venv/bin/python make_news_pool.py      （cloud_collect.sh 采集成功后自动调用）
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import write_brief

BASE = Path(__file__).resolve().parent
OUT = BASE / "news_pool.json"


def main() -> int:
    snapshot = json.loads((BASE / "market_snapshot.json").read_text(encoding="utf-8"))
    window = snapshot.get("news_window") or {}
    start, end = write_brief.parse_iso(window.get("start")), write_brief.parse_iso(window.get("end"))
    if not start or not end:
        sys.exit("✗ market_snapshot.json 缺有效 news_window")
    pool = write_brief.build_news_pool(start, end)
    if not pool:
        sys.exit("✗ 新闻池为空：24 小时窗口内没有任何新闻")
    items = [{"id": i, **item} for i, item in enumerate(pool, 1)]
    OUT.write_text(json.dumps({
        "window_start": window["start"],
        "window_end": window["end"],
        "item_count": len(items),
        "tiers": "1=海外一手(Bloomberg/Reuters/CNBC) 2=国内实时快讯 3=国内热榜",
        "items": items,
    }, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    counts = {t: sum(1 for it in items if it["tier"] == t) for t in (1, 2, 3)}
    print(f"✓ news_pool.json：{len(items)} 条（海外 {counts[1]} / 国内快讯 {counts[2]} / 国内热榜 {counts[3]}），"
          f"窗口 {window['start']} ~ {window['end']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
