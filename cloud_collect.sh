#!/usr/bin/env bash
# 云端（Claude Code cloud 定时会话）Phase 1 采集：建 venv、并行跑四个采集脚本、关键路径重试。
#
# 与 run_brief.sh Phase 1 口径一致：行情 / 新闻 / 财经日历为关键路径（行情、日历失败重试一次），
# fetch_views 失败只告警。退出码：0=可继续写稿；1=关键采集失败，必须终止并交接 FAILED。
#
# 用法：./cloud_collect.sh            （日志写 logs/collect_YYYYMMDD.log，同时打印摘要）
set -u
PROJ_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$PROJ_DIR"
DAY="$(TZ=Asia/Shanghai date +%Y%m%d)"
LOG_DIR="$PROJ_DIR/logs"
LOG="$LOG_DIR/collect_$DAY.log"
mkdir -p "$LOG_DIR"
VENV_PY="$PROJ_DIR/.venv/bin/python"

log() { echo "[$(TZ=Asia/Shanghai date '+%Y-%m-%d %H:%M:%S')] $*" | tee -a "$LOG"; }

if [ ! -x "$VENV_PY" ]; then
  log "创建 venv 并安装依赖"
  python3 -m venv "$PROJ_DIR/.venv" >>"$LOG" 2>&1 && "$VENV_PY" -m pip install -q -r requirements.txt >>"$LOG" 2>&1 \
    || { log "✗ venv/依赖安装失败"; exit 1; }
fi

log "== 云端采集 ${DAY} 开始 =="
"$VENV_PY" fetch_market.py NVDA AMD AAPL MSFT TSLA META MU >>"$LOG" 2>&1 & MKT_PID=$!
"$VENV_PY" fetch_news.py >>"$LOG" 2>&1 & NEWS_PID=$!
"$VENV_PY" fetch_calendar.py >>"$LOG" 2>&1 & CAL_PID=$!
"$VENV_PY" fetch_views.py >"$PROJ_DIR/views_feed.txt" 2>>"$LOG" & VIEWS_PID=$!
wait "$MKT_PID";  MKT_RC=$?
wait "$NEWS_PID"; NEWS_RC=$?
wait "$CAL_PID";  CAL_RC=$?
wait "$VIEWS_PID"; VIEWS_RC=$?

if [ "$MKT_RC" -ne 0 ]; then
  log "fetch_market 首次失败(rc=$MKT_RC)，重试一次"
  "$VENV_PY" fetch_market.py NVDA AMD AAPL MSFT TSLA META MU >>"$LOG" 2>&1; MKT_RC=$?
fi
if [ "$CAL_RC" -ne 0 ]; then
  log "fetch_calendar 首次失败(rc=$CAL_RC)，重试一次"
  "$VENV_PY" fetch_calendar.py >>"$LOG" 2>&1; CAL_RC=$?
fi
[ "$VIEWS_RC" -ne 0 ] && log "⚠️ fetch_views 失败(rc=$VIEWS_RC)，飞书观点不可用，继续"

log "退出码：market=$MKT_RC news=$NEWS_RC calendar=$CAL_RC views=$VIEWS_RC"
FAILED=""
[ "$MKT_RC" -ne 0 ] && FAILED="$FAILED 行情"
[ "$NEWS_RC" -ne 0 ] && FAILED="$FAILED 新闻"
[ "$CAL_RC" -ne 0 ] && FAILED="$FAILED 财经日历"
if [ -n "$FAILED" ]; then
  log "✗ 关键采集失败：$FAILED"
  exit 1
fi
log "✓ 采集完成，可以写稿（日志 $LOG）"
