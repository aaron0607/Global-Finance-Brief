# 云端写稿 + 服务器发布（生产调度）

2026-09-27 起的生产分工：

| 环节 | 在哪里 | 触发 | 入口 |
|---|---|---|---|
| 采集 + 写稿 + 构建 + 终检 | Claude Code cloud 定时会话（Routine） | 北京时间每日 08:20（cron `20 0 * * *` UTC） | `cloud_collect.sh` → Claude 按 AGENTS.md 写 `content_YYYYMMDD.json` → `build_brief.py` → `cloud_handoff.py` |
| 交接 | GitHub `aaron0607/Global-Finance-Brief` 的 `briefs` 分支 | 云端写完即推送 | `briefs/YYYYMMDD/{content_*.json, wechat_article_*.html, READY.json}`；失败时 `FAILED.txt` |
| 发布公众号草稿 + 飞书通知 | 腾讯云服务器 `115.159.56.153`（微信 API IP 白名单在这台） | cron 每 5 分钟（08:00–10:55） | `server_publish.sh` → `publish_wechat.py` |

为什么经 GitHub 交接：云端容器出站只有 HTTPS 代理，实测连不上服务器 SSH 22 端口；服务器只需要能 `git fetch` GitHub。

## 安全边界

- 云端**绝不**运行 `publish_wechat.py`，也不接触微信 AppSecret；草稿只在服务器创建。
- 服务器每天只建一次草稿：`logs/publish_state/DAY.published` / `DAY.publish_failed` 存在即不再发布；发布失败不重试（AGENTS.md：失败时不得重复创建）。
- `cloud_handoff.py` 拒绝覆盖同日已有的不同 `READY.json`；修订已发布草稿仍走 `publish_wechat.py --update`。
- 服务器侧告警（飞书，每天同类只发一次）：云端推送 `FAILED.txt`、截至 10:30 未收到成稿、拉取 GitHub 失败、发布失败。

## 上线步骤（顺序不能乱，避免两个发布者或空跑）

1. **合并代码到 `main`**：Routine 每次运行 `origin/main` 的最新代码；`cloud_collect.sh` / `cloud_handoff.py` 必须已在 main。
2. **云端环境密钥**（环境设置 → Edit → 环境变量，不要贴到对话里）：
   - `JIN10_MCP_TOKEN`（金十快讯与日历，可选但建议）
   - `LARK_APP_ID`、`LARK_APP_SECRET`（`fetch_views.py` 读飞书观点；缺失时写稿没有方向参考）
   - 云端写稿不需要 `DEEPSEEK_API_KEY`。
3. **服务器**：
   ```bash
   git clone https://github.com/aaron0607/Global-Finance-Brief /home/ubuntu/brief-inbox   # 私有仓库改用只读 deploy key + SSH 地址
   cd /home/ubuntu/finance-brief && git pull   # 或本机 git push production main，拿到 server_publish.sh
   crontab -e
   #   删除原来的：20 8 * * * ... run_brief.sh ...
   #   新增：
   #   TZ=Asia/Shanghai
   #   */5 8-10 * * * mkdir -p /home/ubuntu/finance-brief/logs && /home/ubuntu/finance-brief/server_publish.sh >> /home/ubuntu/finance-brief/logs/publish_cron.log 2>&1
   ```
   `run_brief.sh` 保留为应急手动通道（云端整天不可用时在服务器手动跑，DeepSeek 写稿），不再挂 cron。
4. **启用 Routine**（名称「财经简报每日写稿」），首日观察：云端会话日志、`briefs` 分支、服务器 `logs/publish_YYYYMMDD.log`、飞书通知。

## Routine 提示词（每次触发新开会话，原样使用）

```text
你是「财经简报」每日自动写稿任务，运行在 Claude Code cloud 定时会话里（北京时间 08:20 触发）。用户已授权本任务：采集 → 写稿 → 构建 → 把成稿推送到 GitHub 仓库 aaron0607/Global-Finance-Brief 的 briefs 分支。公众号草稿由腾讯云服务器自动发布：你绝不运行 publish_wechat.py，不修改、不提交任何代码，不开 PR。

1. 代码：进入该仓库工作副本（当前目录没有就 git clone https://github.com/aaron0607/Global-Finance-Brief 并进入），执行 git fetch origin main briefs && git checkout --detach origin/main。DAY 为北京时间当天 YYYYMMDD（TZ=Asia/Shanghai date +%Y%m%d）。
2. 通读 AGENTS.md 全文和 content_template.json，之后严格按 AGENTS.md 执行。
3. 运行 ./cloud_collect.sh。退出码非 0：运行 .venv/bin/python cloud_handoff.py $DAY --failed "<失败环节 + 日志关键报错>" 后结束。
4. 按 AGENTS.md「日常流程」第 2-4 步与「内容规则」写稿：新闻池以 news_feed.json 为准（海外一手媒体优先），WebSearch/WebFetch 只做核验与补充且遵守 5 组上限；views_feed.txt 只取多空方向；用 Python json.dump 生成 content_$DAY.json；运行 .venv/bin/python build_brief.py content_$DAY.json，最多构建 3 次，并完成成稿终检。
5. 构建与终检通过：运行 .venv/bin/python cloud_handoff.py $DAY。3 次构建仍不合格或任何硬校验失败：运行 cloud_handoff.py $DAY --failed "<原因>"。
6. 若 git 推送 briefs 分支被拒绝：改用 GitHub MCP 工具向 aaron0607/Global-Finance-Brief 的 briefs 分支提交同样的文件（briefs/$DAY/content_$DAY.json、wechat_article_$DAY.html、READY.json，READY.json 字段同 cloud_handoff.py）；仍失败就在会话里写明原因。
7. 最后用 3-5 行汇报：主标题、正文字数、构建次数、交接结果（READY 或 FAILED 及原因）。
```
