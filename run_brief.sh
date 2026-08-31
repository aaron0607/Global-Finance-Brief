#!/usr/bin/env bash
# 每日财经简报服务器编排脚本（DeepSeek 写稿 + cron）
#
# 职责分工：
#   Phase 1 采集（本脚本并行跑 fetch_market / fetch_news / fetch_calendar / fetch_views）
#   Phase 2 写稿（write_brief.py 调 DeepSeek API 产出 content_YYYYMMDD.json，含结构/字数/强调色自动校验与重试）
#   Phase 3 构建（build_brief.py 硬校验 + HTML 成稿终检）
#   Phase 4 发布（publish_wechat.py，服务器上本机执行 node）
#
# 服务器 crontab 示例（服务器时区设为 Asia/Shanghai，或在 cron 行加 TZ=Asia/Shanghai）：
#   TZ=Asia/Shanghai
#   20 8 * * * mkdir -p /home/ubuntu/finance-brief/logs && /home/ubuntu/finance-brief/run_brief.sh >> /home/ubuntu/finance-brief/logs/cron.log 2>&1
#   ⚠️ 必须先 mkdir -p logs 再重定向：logs/ 缺失时 shell 在打开重定向即失败，脚本不会执行且 cron 静默丢弃错误（2026-08-07 事故）。
#
# 密钥：从 $PROJ_DIR/.secrets.env 读取（KEY=VALUE，chmod 600），
#       至少需要 DEEPSEEK_API_KEY；JIN10_MCP_TOKEN 可选（未配置则新闻池不含金十）。
set -u
PROJ_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$PROJ_DIR"
VENV_PY="$PROJ_DIR/.venv/bin/python"
DAY="$(TZ=Asia/Shanghai date +%Y%m%d)"
LOG_DIR="$PROJ_DIR/logs"
LOG="$LOG_DIR/brief_$DAY.log"
mkdir -p "$LOG_DIR"
if [ -f "$PROJ_DIR/.secrets.env" ]; then
  set -a
  # shellcheck disable=SC1091
  . "$PROJ_DIR/.secrets.env"
  set +a
fi

log() { echo "[$(TZ=Asia/Shanghai date '+%Y-%m-%d %H:%M:%S')] $*" | tee -a "$LOG"; }

notify() { # 飞书机器人通知（服务器配置 lark-bot-qsmp 时有效；失败静默）
  local msg="$1"
  if command -v lark-cli >/dev/null 2>&1 && [ -d /home/ubuntu/lark-bot-qsmp ]; then
    HOME=/home/ubuntu/lark-bot-qsmp lark-cli im +messages-send --as bot \
      --user-id ou_1a823bbae5d58fe27620c8a6add2d891 --text "$msg" >/dev/null 2>&1 || true
  fi
}

log "== 每日财经简报 ${DAY} 开始 =="

# ── Phase 1：并行采集 ──────────────────────────────
log "Phase 1 采集启动"
"$VENV_PY" "$PROJ_DIR/fetch_market.py" NVDA AMD AAPL MSFT TSLA META MU >>"$LOG" 2>&1 & MKT_PID=$!
"$VENV_PY" "$PROJ_DIR/fetch_news.py" >>"$LOG" 2>&1 & NEWS_PID=$!
"$VENV_PY" "$PROJ_DIR/fetch_calendar.py" >>"$LOG" 2>&1 & CAL_PID=$!
"$VENV_PY" "$PROJ_DIR/fetch_views.py" >"$PROJ_DIR/views_feed.txt" 2>>"$LOG" & VIEWS_PID=$!
wait "$MKT_PID";  MKT_RC=$?
wait "$NEWS_PID"; NEWS_RC=$?
wait "$CAL_PID";  CAL_RC=$?
wait "$VIEWS_PID"; VIEWS_RC=$?

# 行情是关键路径：失败只允许重试一次，仍失败即终止，禁止补写或沿用旧快照。
if [ "$MKT_RC" -ne 0 ]; then
  log "fetch_market 首次失败(rc=$MKT_RC)，重试一次"
  "$VENV_PY" "$PROJ_DIR/fetch_market.py" NVDA AMD AAPL MSFT TSLA META MU >>"$LOG" 2>&1
  MKT_RC=$?
fi
if [ "$MKT_RC" -ne 0 ]; then
  log "✗ fetch_market 失败，终止"; notify "⚠️ 财经简报 ${DAY} 失败：行情获取失败"; exit 1
fi
if [ "$NEWS_RC" -ne 0 ]; then
  log "✗ fetch_news 失败，终止"; notify "⚠️ 财经简报 ${DAY} 失败：新闻获取失败"; exit 1
fi
# 本周财经日历同样是关键路径：空表或错日期曾直接进成稿，重试一次仍失败即终止。
if [ "$CAL_RC" -ne 0 ]; then
  log "fetch_calendar 首次失败(rc=$CAL_RC)，重试一次"
  "$VENV_PY" "$PROJ_DIR/fetch_calendar.py" >>"$LOG" 2>&1
  CAL_RC=$?
fi
if [ "$CAL_RC" -ne 0 ]; then
  log "✗ fetch_calendar 失败，终止"; notify "⚠️ 财经简报 ${DAY} 失败：本周财经日历获取失败"; exit 1
fi
if [ "$VIEWS_RC" -ne 0 ]; then
  log "⚠️ fetch_views 失败(rc=$VIEWS_RC)，飞书观点不可用，继续"
fi

# ── Phase 2：DeepSeek 写稿（只产出 JSON；字数/来源校验与重试在脚本内）────
log "Phase 2 写稿启动（初稿 + 结构/字数/强调色定向自动修复，可能耗时数分钟）"
VIEWS_ARG=""
[ -s "$PROJ_DIR/views_feed.txt" ] && VIEWS_ARG="--views $PROJ_DIR/views_feed.txt"
timeout 2100 "$VENV_PY" "$PROJ_DIR/write_brief.py" "$DAY" $VIEWS_ARG >>"$LOG" 2>&1
WRITE_RC=$?
if [ "$WRITE_RC" -eq 124 ]; then
  log "✗ 写稿超过 2100 秒限时"
  notify "⚠️ 财经简报 ${DAY} 失败：写稿与自动修复超时（35分钟）"
  exit 1
fi
if [ "$WRITE_RC" -eq 2 ]; then
  log "✗ 模型已返回 JSON，但写稿内部结构/字数/强调色校验未通过"
  notify "⚠️ 财经简报 ${DAY} 失败：模型已返回 JSON，但结构/字数/强调色校验未通过"
  exit 1
fi
if [ "$WRITE_RC" -ne 0 ]; then
  log "✗ 写稿 API 调用或响应解析失败(rc=$WRITE_RC)"
  notify "⚠️ 财经简报 ${DAY} 失败：写稿 API 调用或响应解析异常"
  exit 1
fi
if [ ! -s "$PROJ_DIR/content_${DAY}.json" ]; then
  log "✗ write_brief 返回成功但 content_${DAY}.json 不存在"
  notify "⚠️ 财经简报 ${DAY} 失败：写稿器内部异常，成功返回但文件缺失"
  exit 1
fi

# ── Phase 3：构建（build_brief 硬校验，失败即停止，不发布）────
log "Phase 3 构建"
"$VENV_PY" "$PROJ_DIR/build_brief.py" "$PROJ_DIR/content_${DAY}.json" >>"$LOG" 2>&1
BUILD_RC=$?
if [ "$BUILD_RC" -ne 0 ]; then
  log "✗ build_brief 失败(rc=$BUILD_RC)"; notify "⚠️ 财经简报 ${DAY} 失败：构建校验未通过"; exit 1
fi

# ── Phase 4：发布（每日自动任务已获授权，创建当天草稿一次）────
log "Phase 4 发布"
"$VENV_PY" "$PROJ_DIR/publish_wechat.py" "$DAY" >>"$LOG" 2>&1
PUB_RC=$?
if [ "$PUB_RC" -ne 0 ]; then
  log "✗ publish_wechat 失败(rc=$PUB_RC)"; notify "⚠️ 财经简报 ${DAY} 失败：发布未完成（请勿重复建稿）"; exit 1
fi

log "✓ 财经简报 ${DAY} 完成：${PROJ_DIR}/content_${DAY}.json / wechat_article_${DAY}.html"
notify "✓ 财经简报 ${DAY} 已创建公众号草稿，请到后台确认后发表"
