#!/usr/bin/env bash
# 腾讯云服务器「只发布」脚本：经 GitHub REST API 轮询 briefs 分支，拿到云端当日成稿后建公众号草稿（每天只建一次）。
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
# 为什么走 API：服务器（腾讯云国内）连 github.com:443 超时，git clone/fetch 不可用；api.github.com 可达。
# 一次性准备：在 .secrets.env（chmod 600）写入 BRIEF_GITHUB_TOKEN=<只读 fine-grained token>，
#   权限只给本仓库 Contents: Read-only。代码同步见 server_sync.py。
#
# 状态文件（logs/publish_state/）：DAY.published / DAY.publish_failed / DAY.alerted，保证幂等与只告警一次。
set -u
PROJ_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$PROJ_DIR"
VENV_PY="$PROJ_DIR/.venv/bin/python"
REPO="${BRIEF_REPO:-aaron0607/Global-Finance-Brief}"
API="${BRIEF_GITHUB_API:-https://api.github.com}"
if [ -f "$PROJ_DIR/.secrets.env" ]; then
  set -a
  # shellcheck disable=SC1091
  . "$PROJ_DIR/.secrets.env"
  set +a
fi
TOKEN="${BRIEF_GITHUB_TOKEN:-}"
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

if [ -z "$TOKEN" ]; then
  alert_once alerted "⚠️ 财经简报 ${DAY} 失败：服务器 .secrets.env 未配置 BRIEF_GITHUB_TOKEN，无法读取云端成稿"
  exit 1
fi

gh_get() { # gh_get <仓库内路径> <输出文件>：打印 HTTP 状态码（200=取到，404=尚无，其它=网络/鉴权问题）
  curl -s -m 30 -o "$2" -w '%{http_code}' \
    -H "Authorization: Bearer $TOKEN" -H "Accept: application/vnd.github.raw+json" \
    -H "X-GitHub-Api-Version: 2022-11-28" \
    "$API/repos/$REPO/contents/$1?ref=briefs"
}

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
READY_CODE="$(gh_get "briefs/$DAY/READY.json" "$TMP/READY.json")"
if [ "$READY_CODE" != "200" ] && [ "$READY_CODE" != "404" ]; then
  log "⚠️ 读取 GitHub briefs 失败(HTTP $READY_CODE)"
  [ "$NOW_HM" -ge "$DEADLINE" ] && alert_once alerted "⚠️ 财经简报 ${DAY} 失败：服务器读取 GitHub 成稿持续失败(HTTP $READY_CODE)，未发布"
  exit 0
fi

if [ "$READY_CODE" = "404" ]; then
  if [ "$(gh_get "briefs/$DAY/FAILED.txt" "$TMP/FAILED.txt")" = "200" ]; then
    REASON="$(tail -n +2 "$TMP/FAILED.txt" | head -c 300)"
    alert_once alerted "⚠️ 财经简报 ${DAY} 失败（云端）：${REASON}"
    exit 0
  fi
  [ "$NOW_HM" -ge "$DEADLINE" ] && alert_once alerted "⚠️ 财经简报 ${DAY} 失败：截至 ${DEADLINE:0:2}:${DEADLINE:2} 未收到云端成稿"
  exit 0
fi

log "收到云端成稿：$(tr -d '\n' <"$TMP/READY.json" | head -c 300)"
for f in "content_${DAY}.json" "wechat_article_${DAY}.html"; do
  CODE="$(gh_get "briefs/$DAY/$f" "$TMP/$f")"
  if [ "$CODE" != "200" ] || [ ! -s "$TMP/$f" ]; then
    log "⚠️ 下载 $f 失败(HTTP $CODE)，下一轮重试"
    [ "$NOW_HM" -ge "$DEADLINE" ] && alert_once alerted "⚠️ 财经简报 ${DAY} 失败：云端成稿 $f 下载失败(HTTP $CODE)"
    exit 0
  fi
done
mv "$TMP/content_${DAY}.json" "$PROJ_DIR/content_${DAY}.json"
mv "$TMP/wechat_article_${DAY}.html" "$PROJ_DIR/wechat_article_${DAY}.html"

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
