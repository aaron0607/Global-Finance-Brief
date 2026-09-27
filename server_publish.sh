#!/usr/bin/env bash
# 腾讯云服务器「只发布」脚本：轮询 GitHub briefs 分支，拿到云端当日成稿后建公众号草稿（每天只建一次）。
#
# 分工：Claude Code cloud 定时会话（北京时间 08:20）采集 + 写稿 + 构建，经 cloud_handoff.py 推到
#       GitHub 的 briefs 分支；本脚本只负责拉取、发布前终检（publish_wechat.py 内置）、建草稿、飞书通知。
#
# 服务器 crontab（替换原 run_brief.sh 那一行，禁止两个发布者并存）：
#   TZ=Asia/Shanghai
#   */5 8 * * * mkdir -p /home/ubuntu/finance-brief/logs && /home/ubuntu/finance-brief/server_publish.sh >> /home/ubuntu/finance-brief/logs/publish_cron.log 2>&1
#   0 9 * * *   mkdir -p /home/ubuntu/finance-brief/logs && /home/ubuntu/finance-brief/server_publish.sh >> /home/ubuntu/finance-brief/logs/publish_cron.log 2>&1
#   （只在北京时间 08:00–09:00 每 5 分钟拉取；当天草稿建好后其余轮次在拉取前直接退出）
#
# 一次性准备：
#   git clone https://github.com/aaron0607/Global-Finance-Brief /home/ubuntu/brief-inbox
#   （私有仓库需给服务器配只读 deploy key 并用 SSH 地址克隆；本脚本只对该目录做 fetch，不改工作区）
#
# 状态文件（logs/publish_state/）：DAY.published / DAY.publish_failed / DAY.alerted，保证幂等与只告警一次。
set -u
PROJ_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$PROJ_DIR"
VENV_PY="$PROJ_DIR/.venv/bin/python"
INBOX="${BRIEF_INBOX:-/home/ubuntu/brief-inbox}"
DEADLINE="${BRIEF_DEADLINE:-0850}"   # 北京时间 HHMM，过点仍无成稿即飞书告警
WINDOW_START="${BRIEF_WINDOW_START:-0800}"  # 轮询窗口（北京时间 HHMM），窗口外直接退出；
WINDOW_END="${BRIEF_WINDOW_END:-0900}"      # 手动补发时设 BRIEF_FORCE=1 跳过窗口限制
DAY="$(TZ=Asia/Shanghai date +%Y%m%d)"
NOW_HM="$(TZ=Asia/Shanghai date +%H%M)"
STATE="$PROJ_DIR/logs/publish_state"
LOG="$PROJ_DIR/logs/publish_$DAY.log"
mkdir -p "$STATE"

if [ "${BRIEF_FORCE:-0}" != "1" ] && { [ "$NOW_HM" -lt "$WINDOW_START" ] || [ "$NOW_HM" -gt "$WINDOW_END" ]; }; then
  exit 0
fi

log() { echo "[$(TZ=Asia/Shanghai date '+%Y-%m-%d %H:%M:%S')] $*" | tee -a "$LOG"; }

notify() { # 飞书机器人通知（与 run_brief.sh 相同；失败静默）
  local msg="$1"
  if command -v lark-cli >/dev/null 2>&1 && [ -d /home/ubuntu/lark-bot-qsmp ]; then
    HOME=/home/ubuntu/lark-bot-qsmp lark-cli im +messages-send --as bot \
      --user-id ou_1a823bbae5d58fe27620c8a6add2d891 --text "$msg" >/dev/null 2>&1 || true
  fi
}

alert_once() { # 同一天同类告警只发一次
  local key="$1" msg="$2"
  [ -e "$STATE/$DAY.$key" ] && return 0
  touch "$STATE/$DAY.$key"
  log "$msg"; notify "$msg"
}

exec 9>"$STATE/.lock"
flock -n 9 || exit 0   # 上一轮还在跑（如正在建草稿）则跳过
[ -e "$STATE/$DAY.published" ] || [ -e "$STATE/$DAY.publish_failed" ] && exit 0

if ! timeout 90 git -C "$INBOX" fetch -q origin briefs >>"$LOG" 2>&1; then
  log "⚠️ 拉取 GitHub briefs 分支失败"
  [ "$NOW_HM" -ge "$DEADLINE" ] && alert_once alerted "⚠️ 财经简报 ${DAY} 失败：服务器拉取 GitHub briefs 分支持续失败，未发布"
  exit 0
fi

REF="origin/briefs:briefs/$DAY"
if git -C "$INBOX" cat-file -e "$REF/FAILED.txt" 2>/dev/null \
    && ! git -C "$INBOX" cat-file -e "$REF/READY.json" 2>/dev/null; then
  REASON="$(git -C "$INBOX" show "$REF/FAILED.txt" | tail -n +2 | head -c 300)"
  alert_once alerted "⚠️ 财经简报 ${DAY} 失败（云端）：${REASON}"
  exit 0
fi

if ! git -C "$INBOX" cat-file -e "$REF/READY.json" 2>/dev/null; then
  [ "$NOW_HM" -ge "$DEADLINE" ] && alert_once alerted "⚠️ 财经简报 ${DAY} 失败：截至 ${DEADLINE:0:2}:${DEADLINE:2} 未收到云端成稿"
  exit 0
fi

log "收到云端成稿：$(git -C "$INBOX" show "$REF/READY.json" | tr -d '\n' | head -c 300)"
for f in "content_${DAY}.json" "wechat_article_${DAY}.html"; do
  if ! git -C "$INBOX" show "$REF/$f" >"$PROJ_DIR/$f.tmp" 2>>"$LOG" || [ ! -s "$PROJ_DIR/$f.tmp" ]; then
    rm -f "$PROJ_DIR/$f.tmp"
    alert_once alerted "⚠️ 财经简报 ${DAY} 失败：云端交接缺少 $f"
    exit 0
  fi
  mv "$PROJ_DIR/$f.tmp" "$PROJ_DIR/$f"
done

# 发布只尝试一次：失败不得重复建稿（AGENTS.md），由人工排查后手动补发或 --update。
log "发布公众号草稿"
"$VENV_PY" "$PROJ_DIR/publish_wechat.py" "$DAY" >>"$LOG" 2>&1
PUB_RC=$?
if [ "$PUB_RC" -ne 0 ]; then
  touch "$STATE/$DAY.publish_failed"
  alert_once alerted "⚠️ 财经简报 ${DAY} 失败：发布未完成(rc=$PUB_RC)（请勿重复建稿，见 logs/publish_$DAY.log）"
  exit 1
fi
touch "$STATE/$DAY.published"
log "✓ 财经简报 ${DAY} 已创建公众号草稿"
notify "✓ 财经简报 ${DAY} 已创建公众号草稿（云端写稿），请到后台确认后发表"
