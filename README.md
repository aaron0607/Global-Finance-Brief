# 股市马斯克 · 每日财经简报（Codex 版）

这是从原 Cloud Code 工作区迁移出的独立可运行副本。原项目没有被改动。

## 环境准备

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

`fetch_views.py` 优先使用 `LARK_APP_ID` / `LARK_APP_SECRET` 走飞书 OpenAPI；未配置时回退到已登录的 `lark-cli`。`write_brief.py`（DeepSeek 写稿）需要 `DEEPSEEK_API_KEY`，`fetch_news.py` 可选使用 `JIN10_MCP_TOKEN`。服务器密钥统一存放在 `.secrets.env`（chmod 600），本机运行需自行导出。公众号发布只适用于已配置 `~/.ssh/lct_cloud` 且具备微信白名单服务器权限的环境；微信密钥只保存在服务器端。

## 快速使用

三个采集命令可以在三个终端并行执行；`fetch_views.py` 的标准输出必须保存为 `views_feed.txt`，才能传入写稿器：

```bash
.venv/bin/python fetch_market.py NVDA AMD AAPL MSFT TSLA META MU
.venv/bin/python fetch_views.py > views_feed.txt
.venv/bin/python fetch_news.py
.venv/bin/python write_brief.py YYYYMMDD --views views_feed.txt   # DeepSeek 生成 content JSON
.venv/bin/python build_brief.py content_YYYYMMDD.json
```

`fetch_market.py` 是行情数据的唯一写入入口；`fetch_views.py` 用于读取最新投资观点，事件本身仍按媒体原意客观呈现。`fetch_news.py` 聚合 alphaear（财联社/华尔街见闻/雪球热榜）、Bloomberg/CNBC RSS、富途牛牛新闻搜索（futu-news-search）与华尔街见闻美股直播，并在配置 `JIN10_MCP_TOKEN` 时并入金十数据快讯，输出 `news_feed.json`。人工研究流程可用 `WebSearch(domain:reuters.com)` 补充 Reuters；服务器 cron 无实时 WebSearch，不含该补充链路。

生成的 `wechat_article_YYYYMMDD.html` 供人工审阅。确认后才运行：

```bash
.venv/bin/python publish_wechat.py YYYYMMDD
```

发布命令会在公众号创建草稿；它不是本地测试步骤。

`build_brief.py` 在 HTML 成稿后会执行独立终检：核对简报日期及文中所有完整日期的真实星期，并拦截“（周）”、`周X`、`X月X日`、`COPY_FROM` 等空值或模板占位符。`publish_wechat.py` 在调用微信接口前会再执行同一终检；任一层失败都禁止建稿或更新草稿。

正文强调色同样是构建与发布硬门禁：alert 至少 2 处，每条 bullet 至少 1 处，`focus` / `ai_tech` 平均每条至少 2 处；单处最多 40 字。`[[red:...]]` 用于上涨、改善或利多，`[[green:...]]` 用于下跌、恶化或利空，`[[c:...]]` 用于关键事实、机构观点、结论和验证条件。写稿器会确定性圈选既有短语补齐遗漏的 `c` 色，不改变可见文字；写稿、构建、发布三个入口共用 `content_validation.py`，手工 JSON 也不能绕过。

字数按读者实际看到的正文计算，`[[c:...]]`、`[[red:...]]`、`[[green:...]]`、`**` 等轻量排版语法本身不计入长度。单段目标用于引导完整论证，结构阶段只拦截少于 100 个可见字符的明显碎段；最终仍以整稿 6,000–7,000 字硬门禁为准，避免把有限重试耗在无编辑价值的个位数字符差上。

正文 bullet 的唯一合法类型是字符串。若模型返回 `{ "text": "...", "emphasis": "..." }`，写稿器只提取 `text`，绝不调用 `str(dict)`；未知对象直接失败。构建器和发布器会通用扫描 JSON 与最终 HTML 中的 Python/JSON 对象字面量、`dict(...)`、`OrderedDict(...)` 等程序结构，不依赖固定字段名；发现残留即阻止生成或更新公众号草稿。

需要修订已经创建的草稿时，使用原草稿的媒体标识更新，不要重复创建：

```bash
.venv/bin/python publish_wechat.py --update YYYYMMDD MEDIA_ID
```

每日自动任务已迁移到云服务器 115.159.56.153，由 cron 于北京时间 08:20 触发 `run_brief.sh`（Phase 1 采集 → `write_brief.py` 调 DeepSeek 写稿 → 构建 → 建公众号草稿 → 飞书通知），已获授权自动创建当天草稿，本机定时任务已停用。`08:20` 不是数据截止点；脚本实际开始的北京时间才是本次 `as_of`。周末与休市日仍创建当天文件；开市标的取本次最新报价，休市标的使用本次快照中的最近交易值。报价时间、市场状态和来源仅用于后台校验，不进入客户侧行情表。

## 标题口径

`main_title` 优先采用两行高信息密度新闻标题：第一行写主事件及直接市场反应，第二行补另一条影响定价的副线，每行最多 24 字。标题事实和数字必须可溯源，禁止“蓄势、机会浮现、买点、静待方向”等观点词。完整规范、正反例和长度规则见 [`docs/title-style.md`](docs/title-style.md)。

正文 JSON 已通过结构校验但标题不合规时，写稿器只单独修复标题；修复仍失败则用已校验行情生成保底标题，不会为标题重新生成整篇。`write_brief.py` 的非零退出码区分故障层级：`1` 表示 API 调用或响应解析失败，`2` 表示已解析到 JSON、但结构、字数或强调色校验仍未通过。服务器日志位于 `logs/brief_YYYYMMDD.log`，飞书通知使用同一分类，不再以“文件是否存在”猜测失败原因。

## 新闻时效

简报以 `fetch_market.py` 实际开始时写入的 `market_snapshot.json.run_started_at` 为 `as_of`，只能使用 `[as_of-24小时, as_of]` 内发布或更新的新闻。新内容 JSON 顶层必须复制该时间：

```json
"as_of": "2026-08-04T08:21:13+08:00"
```

从 `20260727` 起，`content_YYYYMMDD.json` 顶层必须记录来源：

```json
"sources": [
  {
    "source": "Bloomberg",
    "title": "报道标题",
    "published_at": "2026-07-27T05:35:00+08:00",
    "used_in": ["focus", "ai_tech"]
  }
]
```

`published_at` 必须是带时区的 ISO 8601 时间。每个正文板块都要有来源映射；来源早于 `as_of-24小时`、晚于 `as_of`，或无法确认具体发布时间时，生成器会停止构建。新闻来源：金十数据（jin10，配置 `JIN10_MCP_TOKEN` 时）、alphaear 聚合（财联社/华尔街见闻/雪球热榜）、Bloomberg/CNBC RSS、富途牛牛新闻搜索与华尔街见闻美股直播。人工研究流程可通过 `WebSearch(domain:reuters.com)` 补充 Reuters；任务不得连接、搜索或读取 Gmail 及任何邮件来源。

## 行情口径

`fetch_market.py` 使用国内可直连的结构化行情源生成快照（2026-08-06 起免 VPN，不依赖 yfinance/Yahoo）：美股指数/个股用新浪 `gb_$`（`gb_$inx`/`gb_$sox` 等）；黄金/美元现货用新浪 `hf_XAU`；布伦特原油、10 年美债收益率、美元指数用东方财富 `push2delay`（`112.B00Y` / `171.US10Y` / `100.UDI`）；恐慌指数用 CBOE 官方日线 CSV。每项都记录 ticker、报价时间、市场状态、前收盘/前结算参考价和数据来源。交易时段内报价延迟超过 30 分钟、休市报价超过 7 天或任一核心标的缺数时，脚本会写入无效快照并阻止构建。新简报禁止在 content JSON 手填行情，也禁止从新闻、搜索摘要或截图补价。

客户成稿只呈现市场事实、公开数据口径和投资解读，不披露脚本、快照文件、内部文档或后台运行机制。行情总览只显示资产名称、价格和涨跌：指数表头显示实际最近交易日的“X月X日收盘”，大宗/外汇表头显示该组最新有效报价的北京时间日期“X月X日”。两组日期独立更新；周末或休市时沿用最近实际行情日期，不随简报日期自动推进。大宗/外汇表不展示 ticker、逐行报价时间、交易状态或表下行情口径说明，这些审计字段仍完整保留在快照和构建校验中。

服务器自动任务的编排见 `run_brief.sh` 与 `AGENTS.md`「服务器定时运行」；Codex 项目规则以 `AGENTS.md` 为准。内容只写入 `content_YYYYMMDD.json`，禁止直接编辑 HTML。
