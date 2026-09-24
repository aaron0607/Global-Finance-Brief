#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
拉取执行时刻的最新可得行情，并写入可审计的 market_snapshot.json。

数据源（全部为国内可直连、无需 VPN 的固定链接）：
- 美股指数/个股：新浪 hq  `gb_$inx` / `gb_$ixic` / `gb_$dji` / `gb_$sox` / `gb_nvda` ...
- 黄金/美元：新浪 hq `hf_XAU`（伦敦现货 XAU/USD）
- 布伦特原油：东方财富 push2delay `112.B00Y`（当月连续），失败时退回新浪 `hf_OIL`
- 10年美债收益率：东方财富 push2delay `171.US10Y`，失败时退回新浪 `globalbd_us10yt`
- 美元指数：东方财富 push2delay `100.UDI`，失败时退回新浪 `DINIW`
  （东财会对部分云服务器 IP 直接断开 TLS 连接，新浪兜底保证服务器可取数）
- 恐慌指数 VIX：CBOE 官方日线 CSV（cdn.cboe.com，每日收盘）

口径与校验逻辑与原 yfinance 版本保持一致：run_started_at 在脚本真正开始时固化、
动态24h新闻窗口、盘中报价延迟>30分钟或休市报价>7天即无效、单标错误写无效快照、
核心标的缺失禁止构建。只允许使用本脚本结果作为行情口径；禁止从新闻/搜索摘要/截图补值。

用法：
    .venv/bin/python fetch_market.py
    .venv/bin/python fetch_market.py NVDA AMD AAPL MSFT TSLA META MU
"""

from __future__ import annotations

import datetime as dt
import json
import math
import re
import sys
import urllib.request
from pathlib import Path
from zoneinfo import ZoneInfo

SNAPSHOT = Path(__file__).with_name("market_snapshot.json")
BRIEF_TZ = ZoneInfo("Asia/Shanghai")
NY_TZ = ZoneInfo("America/New_York")
UTC = dt.timezone.utc
PROVIDER_SINA = "Sina Finance hq (gb_$)"
PROVIDER_GOLD = "Sina Finance hq (hf_XAU)"
PROVIDER_CBOE = "CBOE official delayed quote JSON"
PROVIDER_CBOE_CSV = "CBOE official daily CSV"
PROVIDER_EM = "EastMoney push2delay quote API"
PROVIDER_SINA_FALLBACK = "Sina Finance hq (%s, EastMoney fallback)"
ACTIVE_MAX_AGE_MINUTES = 30
CLOSED_MAX_AGE_HOURS = 24 * 7
ACTIVE_STATES = {"盘前", "交易中", "盘后"}

# (logical_symbol, 展示名, 资产类型, 数据源)
# 数据源格式: ("sina", "gb_$inx") | ("sina", "gb_<ticker>") | ("sina_hf", "hf_XAU")
#             | ("eastmoney", "<secid>", "<新浪兜底代码>") | ("cboe", None)
CORE = [
    ("^GSPC", "标普500", "index", ("sina", "gb_$inx")),
    ("^IXIC", "纳斯达克综合", "index", ("sina", "gb_$ixic")),
    ("^DJI", "道琼斯", "index", ("sina", "gb_$dji")),
    ("^SOX", "费城半导体", "index", ("sina", "gb_$sox")),
    ("^VIX", "恐慌指数", "index", ("cboe", None)),
    ("XAUUSD", "黄金/美元", "spot_gold", ("sina_hf", "hf_XAU")),
    ("BZ=F", "ICE布伦特原油期货", "commodity", ("eastmoney", "112.B00Y", "hf_OIL")),
    ("^TNX", "10年美债收益率指数", "yield", ("eastmoney", "171.US10Y", "globalbd_us10yt")),
    ("DX-Y.NYB", "ICE美元指数", "fx", ("eastmoney", "100.UDI", "DINIW")),
]
INDEX_SYMS = [symbol for symbol, _, kind, _ in CORE if kind == "index"]
PRICE_INDEX_SYMS = ["^GSPC", "^IXIC", "^DJI", "^SOX"]
VIX_SYMBOL = "^VIX"
VIX_MAX_LAG_CALENDAR_DAYS = 3
COMMODITY_SYMS = [symbol for symbol, _, kind, _ in CORE if kind != "index"]
SINA_HEADERS = {
    "User-Agent": "Mozilla/5.0 (finance-brief/1.0)",
    "Referer": "https://finance.sina.com.cn/",
}
EM_URL = "https://push2delay.eastmoney.com/api/qt/stock/get?secid={secid}&fltt=2&invt=2&fields=f43,f44,f45,f46,f57,f58,f60,f86"
CBOE_VIX_QUOTE_URL = "https://cdn.cboe.com/api/global/delayed_quotes/quotes/_VIX.json"
CBOE_VIX_HISTORY_URL = "https://cdn.cboe.com/api/global/us_indices/daily_prices/VIX_History.csv"


def now_bjt() -> dt.datetime:
    return dt.datetime.now(BRIEF_TZ).replace(microsecond=0)


def iso_bjt(value: dt.datetime) -> str:
    return value.astimezone(BRIEF_TZ).isoformat(timespec="seconds")


def finite_number(value) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def http_text(url: str, headers: dict | None = None, timeout: int = 12) -> str:
    request = urllib.request.Request(url, headers=headers or {})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read().decode("utf-8", "ignore")


def us_market_state(at: dt.datetime) -> str:
    """按执行时刻的纽约时段推导美股盘前/交易中/盘后/休市。"""
    ny = at.astimezone(NY_TZ)
    if ny.weekday() >= 5:
        return "休市"
    t = ny.time()
    if dt.time(9, 30) <= t <= dt.time(16, 0):
        return "交易中"
    if dt.time(4, 0) <= t < dt.time(9, 30):
        return "盘前"
    if dt.time(16, 0) < t <= dt.time(20, 0):
        return "盘后"
    return "休市"


def index_state(quote_time: dt.datetime, run_started_at: dt.datetime) -> str:
    """指数状态：报价若属上一交易日（如盘前时段新浪仍给昨日收盘），
    按「休市·最近交易值」处理，避免把昨日收盘误判为盘前活跃而触发 30 分钟时效。"""
    if quote_time.astimezone(NY_TZ).date() < run_started_at.astimezone(NY_TZ).date():
        return "休市"
    return us_market_state(run_started_at)


def commodity_open(at: dt.datetime) -> bool:
    """布油/美元指数/美债收益率等周内近似连续交易的标的：周一06:00至周六06:00开盘。"""
    local = at.astimezone(BRIEF_TZ)
    weekday = local.weekday()
    if weekday in {1, 2, 3, 4}:
        return True
    if weekday == 0:
        return local.time() >= dt.time(6, 0)
    if weekday == 5:
        return local.time() < dt.time(6, 0)
    return False


def check_freshness(run_started_at: dt.datetime, quote_time: dt.datetime, state: str, what: str) -> float:
    age = (run_started_at - quote_time).total_seconds() / 60
    if age < -5:
        raise RuntimeError("%s 报价时间晚于执行时刻 %.1f 分钟" % (what, -age))
    if state in ACTIVE_STATES and age > ACTIVE_MAX_AGE_MINUTES:
        raise RuntimeError("%s 市场%s但报价已延迟 %.1f 分钟" % (what, state, age))
    if state == "休市" and age > CLOSED_MAX_AGE_HOURS * 60:
        raise RuntimeError("%s 休市报价已超过%d小时" % (what, CLOSED_MAX_AGE_HOURS))
    return age


def make_row(symbol: str, requested_symbol: str, name: str, kind: str, price: float,
             previous_close: float, quote_time: dt.datetime, session_date: str,
             state: str, change_basis: str, provider: str, price_field: str) -> dict:
    if kind == "yield":
        change_value = (price - previous_close) * 100
        change_text = "%+.1f bp" % change_value
        price_text = "%.2f%%" % price
    else:
        change_value = (price - previous_close) / previous_close * 100
        change_text = "%+.2f%%" % change_value
        price_text = f"{price:,.2f}"
    return {
        "symbol": symbol,
        "requested_symbol": requested_symbol,
        "name": name,
        "asset_type": kind,
        "price": price_text,
        "price_raw": round(price, 8),
        "reference_price_raw": round(previous_close, 8),
        "chg": change_text,
        "dir": "down" if change_value < 0 else "up",
        "change_basis": change_basis,
        "quote_time": iso_bjt(quote_time),
        "as_of": quote_time.astimezone(BRIEF_TZ).strftime("%m-%d %H:%M"),
        "market_state": state,
        "quote_age_minutes": round(max((now_bjt() - quote_time).total_seconds() / 60, 0), 1),
        "session_date": session_date,
        "provider": provider,
        "price_field": price_field,
    }


def fetch_sina_us(symbol_code: str, name: str, kind: str, run_started_at: dt.datetime) -> dict:
    """新浪美股指数/个股 gb_ 行情。字段: [1]价 [2]涨跌% [3]报价时间(北京) [4]涨跌额。"""
    url = "https://hq.sinajs.cn/list=" + symbol_code
    request = urllib.request.Request(url, headers=SINA_HEADERS)
    with urllib.request.urlopen(request, timeout=12) as response:
        text = response.read().decode("gb18030", "ignore")
    match = re.search(r'hq_str_%s="([^"]*)"' % re.escape(symbol_code), text)
    if not match or not match.group(1):
        raise RuntimeError("新浪 %s 返回为空（可能该代码不在源内）" % symbol_code)
    fields = match.group(1).split(",")
    if len(fields) < 27:
        raise RuntimeError("新浪 %s 字段不完整" % symbol_code)
    price = finite_number(fields[1])
    change_amount = finite_number(fields[4])
    if price is None or price <= 0 or change_amount is None:
        raise RuntimeError("新浪 %s 最新价/涨跌额无效" % symbol_code)
    previous_close = price - change_amount
    if previous_close <= 0:
        raise RuntimeError("新浪 %s 昨收无效" % symbol_code)
    try:
        quote_time = dt.datetime.strptime(fields[3].strip(), "%Y-%m-%d %H:%M:%S").replace(tzinfo=BRIEF_TZ)
    except ValueError as exc:
        raise RuntimeError("新浪 %s 报价时间无效" % symbol_code) from exc
    state = index_state(quote_time, run_started_at)
    age = check_freshness(run_started_at, quote_time, state, symbol_code)
    session_date = quote_time.astimezone(NY_TZ).date().isoformat()
    change_basis = "较前收盘"
    return make_row(
        symbol_code, symbol_code, name, kind, price, previous_close, quote_time,
        session_date, state, change_basis, PROVIDER_SINA,
        "%s[1]（昨收=[1]-[4]）" % symbol_code,
    )


def fetch_spot_gold(run_started_at: dt.datetime) -> dict:
    """Sina hf_XAU: 伦敦现货黄金（XAU/USD），单位美元/盎司。字段0最新价、字段7昨收、字段6+12北京报价时间。"""
    request = urllib.request.Request(
        "https://hq.sinajs.cn/list=hf_XAU",
        headers={
            "User-Agent": "Mozilla/5.0 (finance-brief/1.0)",
            "Referer": "https://finance.sina.com.cn/",
        },
    )
    with urllib.request.urlopen(request, timeout=12) as response:
        text = response.read().decode("gb18030", "ignore")
    match = re.search(r'hq_str_hf_XAU="([^"]+)"', text)
    if not match:
        raise RuntimeError("新浪 hf_XAU 返回格式无效")
    fields = match.group(1).split(",")
    if len(fields) < 13:
        raise RuntimeError("新浪 hf_XAU 字段不完整")
    price = finite_number(fields[0])
    previous_close = finite_number(fields[7])
    if price is None or price <= 0 or previous_close is None or previous_close <= 0:
        raise RuntimeError("新浪 hf_XAU 最新价/昨收无效")
    try:
        quote_time = dt.datetime.strptime(
            "%s %s" % (fields[12].strip(), fields[6].strip()),
            "%Y-%m-%d %H:%M:%S",
        ).replace(tzinfo=BRIEF_TZ)
    except ValueError as exc:
        raise RuntimeError("新浪 hf_XAU 报价时间无效") from exc

    state = "交易中" if commodity_open(run_started_at) else "休市"
    check_freshness(run_started_at, quote_time, state, "现货黄金")
    change_value = (price - previous_close) / previous_close * 100
    return {
        "symbol": "hf_XAU",
        "requested_symbol": "XAUUSD",
        "name": "黄金/美元（XAU/USD）",
        "asset_type": "spot_gold",
        "price": f"{price:,.2f}",
        "price_raw": round(price, 8),
        "reference_price_raw": round(previous_close, 8),
        "chg": "%+.2f%%" % change_value,
        "dir": "down" if change_value < 0 else "up",
        "change_basis": "较前收盘",
        "quote_time": iso_bjt(quote_time),
        "as_of": quote_time.strftime("%m-%d %H:%M"),
        "market_state": state,
        "quote_age_minutes": round(max((run_started_at - quote_time).total_seconds() / 60, 0), 1),
        "session_date": quote_time.date().isoformat(),
        "provider": PROVIDER_GOLD,
        "price_field": "hf_XAU[0]",
        "reference_field": "hf_XAU[7]",
        "unit": "USD/oz",
    }


def fetch_cboe_vix_quote(run_started_at: dt.datetime) -> dict:
    """CBOE 官方实时延时报价 JSON。收盘（16:15 ET）后立即带最新收盘值，
    避免历史 CSV 每日发布滞后（约收盘后 5 小时、晚于 08:20 采集）导致 VIX 落后价格指数一个交易日。"""
    payload = json.loads(http_text(CBOE_VIX_QUOTE_URL, timeout=15))
    data = payload.get("data") or {}
    price = finite_number(data.get("current_price"))
    if price is None or price <= 0:
        price = finite_number(data.get("close"))
    change = finite_number(data.get("price_change"))
    if price is None or price <= 0 or change is None:
        raise RuntimeError("CBOE VIX 实时报价字段无效")
    # prev_day_close 字段实测会镜像 close（不可信），改由 current_price - price_change 推导昨收。
    previous_close = price - change
    if previous_close <= 0:
        raise RuntimeError("CBOE VIX 实时报价昨收无效")
    raw_time = str(data.get("last_trade_time") or "").strip()
    if not raw_time:
        raise RuntimeError("CBOE VIX 实时报价缺 last_trade_time")
    try:
        naive = dt.datetime.strptime(raw_time, "%Y-%m-%dT%H:%M:%S")
    except ValueError as exc:
        raise RuntimeError("CBOE VIX 实时报价时间无效") from exc
    quote_time = naive.replace(tzinfo=NY_TZ).astimezone(BRIEF_TZ)
    quote_date = naive.date()
    state = index_state(quote_time, run_started_at)
    check_freshness(run_started_at, quote_time, state, "恐慌指数(VIX)")
    return make_row(
        "^VIX", "^VIX", "恐慌指数", "index", price, previous_close, quote_time,
        quote_date.isoformat(), state, "较前收盘", PROVIDER_CBOE,
        "delayed_quotes _VIX.json current_price（昨收=current_price-price_change）",
    )


def fetch_cboe_vix_history(run_started_at: dt.datetime) -> dict:
    """CBOE 官方 VIX 日线 CSV 兜底。取最后两行 CLOSE 作为最新收盘与昨收；
    发布滞后时会落后价格指数一个交易日，由上层 VIX 滞后校验容错。"""
    text = http_text(CBOE_VIX_HISTORY_URL, timeout=15)
    rows = [line.strip() for line in text.splitlines() if line.strip() and not line.startswith("DATE,")]
    if len(rows) < 2:
        raise RuntimeError("CBOE VIX CSV 数据不足")
    latest = rows[-1].split(",")
    prev = rows[-2].split(",")
    price = finite_number(latest[4])
    previous_close = finite_number(prev[4])
    if price is None or price <= 0 or previous_close is None or previous_close <= 0:
        raise RuntimeError("CBOE VIX CSV 收盘值无效")
    try:
        quote_date = dt.datetime.strptime(latest[0].strip(), "%m/%d/%Y").date()
    except ValueError as exc:
        raise RuntimeError("CBOE VIX CSV 日期无效") from exc
    # 收盘 16:00 ET → 北京
    quote_time = dt.datetime(quote_date.year, quote_date.month, quote_date.day, 16, 0, tzinfo=NY_TZ).astimezone(BRIEF_TZ)
    state = index_state(quote_time, run_started_at)
    check_freshness(run_started_at, quote_time, state, "恐慌指数(VIX)")
    return make_row(
        "^VIX", "^VIX", "恐慌指数", "index", price, previous_close, quote_time,
        quote_date.isoformat(), state, "较前收盘", PROVIDER_CBOE_CSV, "VIX_History.csv CLOSE",
    )


def fetch_cboe_vix(run_started_at: dt.datetime) -> dict:
    """VIX 优先取实时延时报价 JSON（无滞后）；失败时退回历史 CSV 兜底。"""
    try:
        return fetch_cboe_vix_quote(run_started_at)
    except Exception as primary_exc:
        try:
            return fetch_cboe_vix_history(run_started_at)
        except Exception as fallback_exc:
            raise RuntimeError(
                "CBOE VIX 实时源与历史源均失败：%s ／ %s" % (primary_exc, fallback_exc)
            ) from fallback_exc


def fetch_eastmoney(secid: str, name: str, kind: str, run_started_at: dt.datetime) -> dict:
    """东方财富 push2delay（延时行情）: f43最新 f44最高 f45最低 f46今开 f60昨收/昨结 f86时间戳。"""
    data = json.loads(http_text(EM_URL.format(secid=secid), timeout=12)).get("data")
    if not data:
        raise RuntimeError("东财 %s 返回空" % secid)
    price = finite_number(data.get("f43"))
    previous_close = finite_number(data.get("f60"))
    ts = finite_number(data.get("f86"))
    if price is None or price <= 0 or previous_close is None or previous_close <= 0:
        raise RuntimeError("东财 %s 最新价/昨收无效" % secid)
    if ts is None:
        raise RuntimeError("东财 %s 缺时间戳" % secid)
    quote_time = dt.datetime.fromtimestamp(ts, UTC).astimezone(BRIEF_TZ)
    state = "交易中" if commodity_open(run_started_at) else "休市"
    check_freshness(run_started_at, quote_time, state, name)
    session_date = quote_time.astimezone(NY_TZ).date().isoformat()
    change_basis = "较前结算" if kind == "commodity" else ("较前收盘（基点）" if kind == "yield" else "较前收盘")
    return make_row(
        secid, secid, name, kind, price, previous_close, quote_time,
        session_date, state, change_basis, PROVIDER_EM, "f43(昨收=f60)",
    )


def sina_fields(code: str) -> list[str]:
    request = urllib.request.Request("https://hq.sinajs.cn/list=" + code, headers=SINA_HEADERS)
    with urllib.request.urlopen(request, timeout=12) as response:
        text = response.read().decode("gb18030", "ignore")
    match = re.search(r'hq_str_%s="([^"]*)"' % re.escape(code), text)
    if not match or not match.group(1):
        raise RuntimeError("新浪 %s 返回为空" % code)
    return match.group(1).split(",")


def fetch_sina_fallback(code: str, name: str, kind: str, run_started_at: dt.datetime) -> dict:
    """东财不可达时的新浪兜底：
    hf_OIL          [0]最新 [7]昨结 [12]日期+[6]时间（北京）
    DINIW           [1]最新 [3]昨收 [10]日期+[0]时间（北京）
    globalbd_us10yt [3]最新收益率 [2]昨收 [11]Unix时间戳
    """
    fields = sina_fields(code)
    try:
        if code == "hf_OIL":
            price, previous_close, field = finite_number(fields[0]), finite_number(fields[7]), "hf_OIL[0]（昨结=[7]）"
            quote_time = dt.datetime.strptime("%s %s" % (fields[12].strip(), fields[6].strip()),
                                              "%Y-%m-%d %H:%M:%S").replace(tzinfo=BRIEF_TZ)
        elif code == "DINIW":
            price, previous_close, field = finite_number(fields[1]), finite_number(fields[3]), "DINIW[1]（昨收=[3]）"
            quote_time = dt.datetime.strptime("%s %s" % (fields[10].strip(), fields[0].strip()),
                                              "%Y-%m-%d %H:%M:%S").replace(tzinfo=BRIEF_TZ)
        elif code == "globalbd_us10yt":
            price, previous_close, field = finite_number(fields[3]), finite_number(fields[2]), "globalbd_us10yt[3]（昨收=[2]）"
            ts = finite_number(fields[11])
            if ts is None:
                raise ValueError("缺时间戳")
            quote_time = dt.datetime.fromtimestamp(ts, UTC).astimezone(BRIEF_TZ)
        else:
            raise RuntimeError("未知新浪兜底代码 %s" % code)
    except (IndexError, ValueError) as exc:
        raise RuntimeError("新浪 %s 字段不完整或时间无效" % code) from exc
    if price is None or price <= 0 or previous_close is None or previous_close <= 0:
        raise RuntimeError("新浪 %s 最新价/昨收无效" % code)
    state = "交易中" if commodity_open(run_started_at) else "休市"
    check_freshness(run_started_at, quote_time, state, name)
    session_date = quote_time.astimezone(NY_TZ).date().isoformat()
    change_basis = "较前结算" if kind == "commodity" else ("较前收盘（基点）" if kind == "yield" else "较前收盘")
    return make_row(
        code, code, name, kind, price, previous_close, quote_time,
        session_date, state, change_basis, PROVIDER_SINA_FALLBACK % code, field,
    )


def validate_closed_index_dates(rows: dict[str, dict]) -> dict:
    price_dates = {rows[symbol]["session_date"] for symbol in PRICE_INDEX_SYMS}
    result = {
        "valid": True,
        "misaligned": len(price_dates) != 1,
        "errors": [],
    }
    if len(price_dates) != 1:
        result["valid"] = False
        result["errors"].append({
            "symbol": "INDEX_GROUP",
            "error": "四个美股价格指数最新交易日不一致: %s" % sorted(price_dates),
        })

    price_date = dt.date.fromisoformat(max(price_dates))
    vix_date = dt.date.fromisoformat(rows[VIX_SYMBOL]["session_date"])
    vix_lag_days = (price_date - vix_date).days
    if vix_lag_days < 0:
        result["valid"] = False
        result["errors"].append({
            "symbol": "INDEX_GROUP",
            "error": "VIX交易日%s晚于价格指数基准日%s" % (vix_date, price_date),
        })
    elif vix_lag_days > VIX_MAX_LAG_CALENDAR_DAYS:
        result["valid"] = False
        result["errors"].append({
            "symbol": "INDEX_GROUP",
            "error": "VIX交易日%s较价格指数基准日%s滞后%d天，超过允许的%d天"
                     % (vix_date, price_date, vix_lag_days, VIX_MAX_LAG_CALENDAR_DAYS),
        })

    result.update({
        "price_index_session_date": price_date.isoformat(),
        "vix_session_date": vix_date.isoformat(),
        "vix_lag_calendar_days": vix_lag_days,
        "index_date_label": (
            "%d月%d日收盘" % (price_date.month, price_date.day)
            if vix_lag_days == 0
            else "%d月%d日收盘（VIX截至%d月%d日）"
                 % (price_date.month, price_date.day, vix_date.month, vix_date.day)
        ),
    })
    return result


def fetch_one(symbol: str, name: str, kind: str, source: tuple, run_started_at: dt.datetime) -> dict:
    source_type, code = source[0], source[1]
    if source_type == "sina":
        return fetch_sina_us(code, name, kind, run_started_at)
    if source_type == "sina_hf":
        return fetch_spot_gold(run_started_at)
    if source_type == "cboe":
        return fetch_cboe_vix(run_started_at)
    if source_type == "eastmoney":
        try:
            return fetch_eastmoney(code, name, kind, run_started_at)
        except Exception as exc:  # noqa: BLE001 - 东财任何失败都尝试新浪兜底
            if len(source) < 3:
                raise
            print("! %-10s 东财失败（%s），改用新浪 %s" % (symbol, exc, source[2]), file=sys.stderr)
            try:
                return fetch_sina_fallback(source[2], name, kind, run_started_at)
            except Exception as fallback_exc:  # noqa: BLE001
                raise RuntimeError("东财失败：%s；新浪兜底失败：%s" % (exc, fallback_exc)) from fallback_exc
    raise RuntimeError("未知数据源类型 %s" % source_type)


def write_snapshot(payload: dict) -> None:
    temp_path = SNAPSHOT.with_suffix(".json.tmp")
    temp_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temp_path.replace(SNAPSHOT)


def main() -> int:
    run_started_at = now_bjt()
    window_start = run_started_at - dt.timedelta(hours=24)
    extra = [(symbol.upper(), symbol.upper(), "stock", ("sina", "gb_%s" % symbol.lower())) for symbol in sys.argv[1:]]
    rows = {}
    errors = []

    for symbol, name, kind, source in CORE + extra:
        try:
            row = fetch_one(symbol, name, kind, source, run_started_at)
            rows[symbol] = row
            print(
                "%-10s %-18s %12s %10s  %s · %s · age %.1fmin"
                % (
                    row["symbol"],
                    row["name"],
                    row["price"],
                    row["chg"],
                    row["as_of"],
                    row["market_state"],
                    row["quote_age_minutes"],
                )
            )
        except Exception as exc:  # noqa: BLE001 - 必须把单标的后端错误写入无效快照
            errors.append({"symbol": symbol, "name": name, "error": str(exc)})
            print("✗ %-10s %-18s %s" % (symbol, name, exc), file=sys.stderr)

    fetched_at = now_bjt()
    missing_core = [symbol for symbol, _, _, _ in CORE if symbol not in rows]
    valid = not missing_core
    snapshot = {
        "schema_version": 2,
        "valid": valid,
        "run_started_at": iso_bjt(run_started_at),
        "news_window": {
            "start": iso_bjt(window_start),
            "end": iso_bjt(run_started_at),
        },
        "fetched_at": iso_bjt(fetched_at),
        "provider": "structured market feeds",
        "providers": sorted({row["provider"] for row in rows.values()}),
        "freshness_policy": {
            "active_max_age_minutes": ACTIVE_MAX_AGE_MINUTES,
            "closed_max_age_hours": CLOSED_MAX_AGE_HOURS,
        },
        "errors": errors,
    }

    if valid:
        index_rows = [rows[symbol] for symbol in INDEX_SYMS]
        commodity_rows = [rows[symbol] for symbol in COMMODITY_SYMS]
        active_indexes = [row for row in index_rows if row["market_state"] in ACTIVE_STATES]
        if active_indexes:
            latest_time = max(dt.datetime.fromisoformat(row["quote_time"]) for row in active_indexes)
            snapshot["index_date_label"] = "指数（截至%s）" % latest_time.strftime("%H:%M")
            snapshot["index_price_label"] = "最新价"
        else:
            alignment = validate_closed_index_dates(rows)
            errors.extend(alignment["errors"])
            if not alignment["valid"]:
                snapshot["valid"] = False
            snapshot["misaligned"] = alignment["misaligned"]
            snapshot["price_index_session_date"] = alignment["price_index_session_date"]
            snapshot["vix_session_date"] = alignment["vix_session_date"]
            snapshot["vix_lag_calendar_days"] = alignment["vix_lag_calendar_days"]
            snapshot["vix_max_lag_calendar_days"] = VIX_MAX_LAG_CALENDAR_DAYS
            snapshot["index_date_label"] = alignment["index_date_label"]
            snapshot["index_price_label"] = "收盘价"
        snapshot["bar_date"] = max(rows[symbol]["session_date"] for symbol in PRICE_INDEX_SYMS)[5:]
        snapshot.setdefault("misaligned", False)
        commodity_quote_date = max(
            dt.datetime.fromisoformat(row["quote_time"]).astimezone(BRIEF_TZ).date()
            for row in commodity_rows
        )
        snapshot["commodity_quote_date"] = commodity_quote_date.isoformat()
        snapshot["commodity_date_label"] = "%d月%d日" % (
            commodity_quote_date.month,
            commodity_quote_date.day,
        )
        snapshot["indices"] = index_rows
        snapshot["commodities"] = commodity_rows
        snapshot["extra_quotes"] = [rows[symbol] for symbol, _, _, _ in extra if symbol in rows]

    write_snapshot(snapshot)
    if not snapshot["valid"]:
        print(
            "\n✗ 核心行情不完整，已写入 invalid 快照：%s；禁止构建/发布。" % SNAPSHOT,
            file=sys.stderr,
        )
        return 1

    print("\n✓ 有效快照已写入 %s" % SNAPSHOT)
    print("新闻窗口: %s 至 %s" % (snapshot["news_window"]["start"], snapshot["news_window"]["end"]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
