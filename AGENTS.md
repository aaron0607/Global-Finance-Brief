# 财经简报（运行手册）

本目录是唯一可修改的工作副本。原 Cloud Code 项目仅用于历史查阅，绝不写入、移动或删除其中的文件。

## ⚠️ 执行前必读（每次任务第一步）

**无论定时任务还是手动执行，第一步永远是阅读本文件全文和 `content_template.json`。** 禁止凭记忆或上次经验直接开始。

1. 确认当天北京时间日期 YYYYMMDD
2. 阅读本文件全文（含下方日常流程、内容规则、数据边界）
3. 阅读 `content_template.json`（含 JSON 结构范例和板块 bullet 数量约束）
4. 确认 `market_snapshot.json` 是否已存在且不超过 2 小时（超过则重新 fetch）
5. 确认 `calendar_feed.json` 是否已存在且不超过 12 小时（超过则重新运行 `fetch_calendar.py`）

## 日常流程

1. 任务启动后立即并行运行四个采集脚本（彼此无依赖，可同时执行）：

   ```bash
   .venv/bin/python fetch_market.py NVDA AMD AAPL MSFT TSLA META MU  # 行情快照
   .venv/bin/python fetch_views.py > views_feed.txt                  # 最近 3 篇飞书观点
   .venv/bin/python fetch_news.py           # Bloomberg/CNBC RSS + Reuters + Futu + 金十 + 华尔街见闻 + alphaear → news_feed.json
   .venv/bin/python fetch_calendar.py       # 金十日历 + 东方财富日历 → calendar_feed.json
   ```

   `fetch_calendar.py` 产出「本周财经日历」候选（见「财经日历数据源」），日历日期一律以它为准，**禁止**凭记忆或新闻推断填写。
   脚本 `fetch_market.py` 会在真正开始时固化北京时间 `run_started_at`，并更新本目录的 `market_snapshot.json`；行情数据源为国内可直连固定链接（见"行情数据源"），不依赖 VPN。`fetch_news.py` 聚合 Bloomberg/CNBC RSS、Reuters（官方 news sitemap，Bing News 兜底）、富途牛牛新闻搜索（futu-news-search，公共 HTTP 接口，无需 OpenD 或鉴权）、金十、华尔街见闻美股直播和 alphaear（财联社/华尔街见闻/雪球热榜）。
2. 采集新闻与观点；行情数值只采用第一步结果。`08:20` 只是定时任务的计划触发时刻，不是内容截止点。以 `market_snapshot.json.run_started_at` 为本次统一 `as_of`，只允许使用 `[as_of-24小时, as_of]` 内发布或更新的新闻。
   - 不得连接、搜索或读取 Gmail 及任何邮件来源；邮件通讯不进入候选池。
   - 仅允许有明确发布时间且带时区的信息进入正文。超过 24 小时、晚于本期截止点、只有日期没有具体时间，或无法确认发布时间的信息一律剔除。
   - **海外一手媒体优先**：Bloomberg/Reuters/CNBC 为一级来源，优先入池（最多 60/100 席）；富途、金十、华尔街见闻美股直播为二级；财联社/华尔街见闻/雪球热榜为三级。同一事件多源报道时，事实、数字、时间以海外一手媒体为准，国内来源只补充 A 股/港股/国内政策视角。
   - 新闻来源固定使用：**Bloomberg/CNBC RSS**；**Reuters 官方 news sitemap**（`fetch_news.py` 已并入，不可达时退回 Bing News `site:reuters.com`）；**金十数据（jin10）快讯+文章列表+关键词快讯搜索**；**alphaear 脚本聚合** 的财联社/华尔街见闻/雪球热榜；**富途牛牛 futu-news-search**（`fetch_news.py` 已并入）；**华尔街见闻·美股直播**（`api-one.wallstcn.com/apiv1/content/lives?channel=us-stock-channel`，`fetch_news.py` 已并入）；**WebSearch**（含 domain:reuters.com）只用于核验新闻池内已有事实的时间、数字与表述。不使用新浪财经新闻搜索（余额不稳定），不派生代理。
   - 搜索全流程最多 5 组（AI/科技与宏观必查，地缘仅有实质进展时查，焦点深挖最多 2 组），同一主题不得换关键词重复搜索。
   - 这 5 组搜索应**一次性并行发出**，不要串行等待。不再批量 WebFetch 文章原文（2026-09-27 实测 11 篇里 10 篇被 CNBC/NPR/富途等站点拦截或无正文，白耗时间）。
   - **正文事实严格只用新闻池**：生产（DeepSeek）由 `write_brief.build_news_pool` 从 `news_feed.json` 构建新闻池（`[as_of-24h, as_of]` 窗口内、去重、按来源分级）；云端写稿通道（已暂停）用 `cloud_collect.sh` 生成的同口径 `news_pool.json`（`make_news_pool.py`）。新闻池是唯一新闻事实来源，`sources` 也只能从中选。WebSearch 搜到但新闻池里没有的事实、数字、条款、人物言论一律不得写入正文，只能用来核对池内事实。
   - 常规简报不得使用代理继续派生代理。
3. 仅编辑 `content_YYYYMMDD.json`；不要手改生成的 HTML。`calendar` 字段直接照抄 `calendar_feed.json` 的 `events`（`date` / `date_iso` / `event` / `stars`），不得增删日期或改写时间。JSON 顶层的 `as_of` 必须原样复制 `market_snapshot.json.run_started_at`，且禁止手写 `indices` / `commodities`。从 `20260727` 起必须在 JSON 顶层维护 `sources`，每项包含 `source`、`title`、带时区的 ISO 8601 `published_at`、以及板块 id 数组 `used_in`；每个正文板块至少映射一个有效来源。
   - **构造 JSON 必须使用 Python `json.dump()`**，不要手写 JSON 字符串——中文弯引号 `””` 与 JSON 半角引号 `””` 视觉相似但解析不兼容，手写极易浪费多轮重试。
   - 可复制 `content_template.json` 作为起点，修改 date/meta/content 后一次生成。
   - 固定板块：focus、ai_tech、stock_rotation、iran（可选，仅实质进展时）、macro、commodity。
   - 每个板块必填 `id` / `title` / `bullets` / `note`。基准结构为 focus 3 条、ai_tech 4 条、stock_rotation 2 条、iran 3 条、macro 3 条、commodity 2 条；无 iran 时 focus/ai_tech 可各增加 1 条，重大信号充足时 stock_rotation/macro 可各增加 1 条。
   - 每条 bullet 是一个完整论证段，不是资讯卡片；commodity 固定两段，第一段写黄金，第二段合并布伦特原油期货与美元指数。
   - 周末可沿用结构；各标的均由本次快照按自身市场状态给出最新可得值，休市标的显示最近交易值。不得复制旧快照、旧行情或过期新闻。
4. 运行 `.venv/bin/python build_brief.py content_YYYYMMDD.json`。生成器会硬性校验动态 24 小时来源窗口、行情快照有效性、快照与 `as_of` 一致性及逐项报价时间/市场状态；校验失败不得生成或发布。
   - HTML 成稿后必须再做一轮独立终检：简报日期及文中所有“年月日（周几）”必须与真实日历一致，不得出现“（周）”“周X”“X月X日”“COPY_FROM”等空值或模板占位符；终检失败禁止发布。`publish_wechat.py` 必须在发布入口再复核一次，防止绕过构建流程直接发布错误 HTML。
   - 初稿目标 6,300–6,600 字，最终硬范围为 6,000–7,000 字。alert 与单段长度均按去掉轻量标记后的读者可见正文计算；单段目标用于引导生成，结构阶段只拦截少于 100 个可见字符的明显碎段，最终篇幅仍以整稿硬范围为准。
   - 首次构建后查看分段字数统计，按比例一次性调整到位（例如 focus 948 字超了，看哪些板块超出目标比例最多就砍哪里）。
   - 写稿时按每条 bullet 约 330–380 个可见字、alert 约 200 字控制篇幅，力求首次构建即落在 6,300–6,600 字。
   - 全程构建最多 3 次。**不要逐字微调**——看到超限优先压缩最大板块的重复背景，欠字优先扩写 ai_tech 或 focus 的交叉验证；仅在不违反 bullet 数量范围时整段增删。
5. 发布只在腾讯云服务器进行。人工运行发布前须取得用户当次确认：`.venv/bin/python publish_wechat.py YYYYMMDD` 会创建公众号草稿并发送飞书通知。已获用户明确授权的每日自动任务可直接发布当天草稿；失败时不得重复创建。
6. 需要修订已有草稿时，使用 `.venv/bin/python publish_wechat.py --update YYYYMMDD MEDIA_ID`；必须更新原草稿，禁止用新建草稿替代修订。

## 内容规则

- 固定板块顺序：focus、ai_tech、stock_rotation、iran（可选）、macro、commodity。
- `ai_tech` 是核心板块；commodity 固定两段（黄金一段；布伦特原油期货与美元指数合并一段）。
- 事件客观呈现；解读用”课代表视角/框架”，不用”用户视角”。
- **人名简称**：成稿中「习近平」一律写作「习」（含标题、导读、正文、逻辑框架、note；如“国家主席习”“习主席”）。`build_brief.py` 构建时按 `content_validation.NAME_ABBREVIATIONS` 自动替换并回写 content（`sources` 来源登记保留新闻原标题），成稿终检与 `publish_wechat.py` 发现残留全称即拦截。
- **主语必须点名，旧闻不得换时间戳混入**：涉及具体公司/机构的事实必须写出名称，严禁用“芯片巨头”“存储龙头”“某公司”等泛称指代单一主体（`write_brief.validate_required` 拦截并打回修复）。`fetch_news.py` 每次采集把标题累积进 `news_history.json`（保留 10 天）；`write_brief.build_news_pool` 入池前剔除两类条目并在日志打印：标题为泛称主语且无摘要的标题党；与窗口外旧新闻共享同一辨识度数字（带“亿/万亿”的金额或带小数的百分比）且标题至少 2 个中文双字重合的旧闻转载。
- **事实与解读分离**：新闻事实（谁、发生了什么、官方数据、公司财报数字）严格按新闻原文客观呈现，原样引用数字，不套用主观立场；若观点本身就来自新闻（机构/官方表态），照写不改。
- **解读必须对齐用户观点（仅方向）**：所有投资解读/观点部分（课代表点评、逻辑判断、路径分析、风险提示，以及对黄金/AI·半导体/地缘/美联储/资金轮动的方向判断）必须对齐飞书「股市马斯克文章发表」**最近 3 篇文章**（最新一篇最权威，观点以最新为准）的多空方向。**飞书文章只提供方向观点，绝不是素材**：`write_brief.py` 先把飞书原文提炼成纯"立场清单"（只保留各主题多空方向与要点，剥离标题、措辞、数字、事件细节），再喂给写稿模型；**严禁**引用飞书标题、措辞、价格/持仓数字或事件表述作为正文内容。
- **新闻内容来源红线**：正文所有新闻事实、数据、事件、人物言论、公司财报数字只能来自新闻池（云端通道（已暂停）为 `news_pool.json`；生产 DeepSeek 通道为 `write_brief.py` 从 `news_feed.json` 构建的新闻池）与 `market_snapshot.json`（行情），WebSearch 结果不得引入池外事实，飞书观点绝不进入新闻事实部分。**正文严禁出现"据XX报道""据报道""有报道称""据报""据XX称"等来源转述/出处表述**——事实直接客观陈述，不标注、不转述新闻来源；来源登记只存在 JSON `sources` 字段（脚本自动挂接），不进入成稿正文。
- **本周财经日历必须来自确定性日历源（防空表与错日期）**：`calendar` 由 `fetch_calendar.py` 产出的 `calendar_feed.json` 回填，模型/人工都不得自行决定日期。日期列固定写成「9月4日 周五」并附 `date_iso`；条目 1-6 条、按日期升序、不得早于简报日期、不得超出 10 天展望窗口。`content_validation.validate_calendar()` 在写稿、构建、发布三个入口硬拦截（空表、星期错配、过期或越界日期、乱序一律失败）。日历源全部不可用时才退回 `sanitize_calendar()` 的新闻池溯源清洗，宁可少、不可错。
- **标题必须是高信息密度新闻播报**：优先两行；第一行写主事件及直接市场反应，第二行写另一条足以影响定价的科技、宏观、地缘或大宗副线。每行可放 1-2 个紧密相关的事实信号、最多 24 字；事实、数字、方向和因果须可溯源。禁止投资观点、预测、问句/感叹号、标题党、来源转述、内部口径和飞书标题/改写。模型标题不合规时只单独修复标题，不得因此丢弃并重写整篇；单独修复仍失败时由行情快照生成合规兜底标题。详见 [`docs/title-style.md`](docs/title-style.md)。
- **正文编辑感**：板块标题必须写出核心矛盾或结论，不能只罗列新闻名词。全文采用约 17 个少而完整的论证段，每段按“事实与数字→市场反应/交叉验证→传导逻辑→风险或验证条件”推进；不要机械地给每段加粗体小标题，不重复背景，不用万能结论凑字。导读控制在 180-240 字，note 控制在 40-90 字。
- **逻辑框架**：三条路径名称随当天事实变化，依次为科技/产业主线（紫 `#534AB7`）、宏观/利率逆风（红 `#A32D2D`）、地缘/黄金/其他独立变量（绿 `#0F6E56`），禁止固定套用“乐观/悲观/备选情景”。
- **内容方向（对齐原项目聚焦）**：主线始终是 **AI 上游超级产业周期**（设备/材料/存储/CPO/PCB/电力冷却等"卖铲子"环节），科技回调=交易层面非基本面逆转；`ai_tech` 为核心板块（算力/芯片、AI前沿、云资本支出、政策/生态）；在 `ai_tech`/`stock_rotation`/`macro` 附 1-2 条当日市场热议的**海外投行/机构研报解读并点名机构**（高盛/摩根士丹利/美银/花旗/Rosenblatt/巴克莱等）；黄金由利率预期主导，地缘经"油价→通胀→利率"链条影响黄金（**禁止写"封锁海峡→黄金上行触发器"**）；美联储路径按最新事实设三种情景（基准/偏好/偏坏），情景内容须与新闻池里的最新政策动作和市场定价一致（如已进入加息周期则围绕加息节奏展开），不再固定套用“不降息/降1次/加1次”；已公布数据（CPI/PPI/NFP）不得作为头条"最新"事件。
- 客户成稿只呈现事实、公开数据口径和投资解读；禁止出现”项目行情快照显示””飞书最新文章”、`market_snapshot`、`fetch_market`、`fetch_views`、”脚本运行”等内部取数、内容管理或后台运行机制表述。参考内部观点时必须直接写成对市场的分析，不交代内部来源。
- 行情总览面向读者只展示资产名称、价格和涨跌。指数表头固定显示实际最近交易日的“X月X日收盘”；大宗/外汇表头显示该组最新有效报价的北京时间日期“X月X日”。两组日期独立随真实行情更新，周末或休市期间不得跟随简报日期推进；VIX 若滞后则单独标注截止日。大宗/外汇名称使用”黄金/美元 / 布伦特原油期货 / 10年美债收益率 / 美元指数”；不得展示 ICE 等非必要前缀、ticker、逐行报价时间、交易状态，也不得在表下添加行情口径说明。完整报价时间、市场状态、ticker 和来源只保留在快照与构建校验中。
- 仅在 JSON 写纯文本和轻量标记；样式、表格、页脚均由 `build_brief.py` 渲染。
- `sections[].bullets[]` 必须是纯文本字符串；严禁把 `{text, emphasis}`、字典/JSON/Python 对象或其字符串表示写入客户正文。写稿器只允许从模型返回的标准 `{ "text": "..." }` 对象中提取 `text`，其他非字符串结构一律硬失败；构建与发布入口还必须通用扫描 Python/JSON 对象字面量，不依赖 `text/emphasis` 等特定字段名，发现结构残留即禁止发布。
- 轻量标记：`**粗体**`、`[[red:涨/利多]]`、`[[green:跌/利空]]`、`[[c:强调色]]`（c 随板块颜色自动变化）。
- **强调色硬规则**：alert 至少 2 处；每条 bullet 至少 1 处，`focus` / `ai_tech` 合计至少达到每条平均 2 处。`red` 只标上涨、改善、利多，`green` 只标下跌、恶化、利空，`c` 标关键事实、机构观点、核心结论或验证条件；每处只包住不超过 40 字的数字或短语，禁止整段着色。`**粗体**` 不计入强调色数量。`write_brief.py` 会在模型遗漏时只圈选既有短语补 `c` 色，不增删或改写可见正文；`build_brief.py`、`publish_wechat.py` 继续使用同一 `content_validation.py` 硬拦截未达标内容。
- 修改渲染器时，微信彩框必须使用 `<section>` 与 `background-color`；不要改成 `<div>`/`<p>` 彩框或 `background` 简写。
- 每次修改流程或脚本，在本目录 `CHANGELOG.md` 顶部记录原因和影响；git 提交信息可以简短，不替代 `CHANGELOG.md` 里的原因与影响说明。
- 本项目使用本地 Git 仓库做版本管理，范围限于代码、规则与模板：`*.py`、`AGENTS.md`、`README.md`、`CHANGELOG.md`、`content_template.json`、`docs/`、`wechat_fragments/`、`requirements.txt`、`run_brief.sh`、`cloud_collect.sh`、`server_publish.sh`。
  - 每日抓取/生成的运行数据与产出物（`content_YYYYMMDD.json`、`wechat_article_YYYYMMDD.html`、`market_snapshot.json`、`news_feed.json`、`calendar_feed.json`、`views_feed.json`、`qdii_cache.json`、`news_history.json`）、`.venv/`、`__pycache__/`、`backups/`、`.secrets.env`、`.claude/settings.local.json` 由 `.gitignore` 排除，不纳入版本管理——这些要么可重新生成，要么已用文件名日期自成历史，要么含机器本地路径或密钥。
  - 提交前用 `git status` 确认暂存内容，不要用 `git add -A`/`git add .` 整批加入，避免误提交上述排除范围。
  - 远程：`origin` = GitHub `aaron0607/Global-Finance-Brief`，`main` 为代码唯一权威版本；改动走分支 + PR，由用户合并。本机 `main` 只做 `git pull --ff-only origin main` 跟随，不在本机 main 上直接提交。

## 典型故障处理

| 症状 | 原因 | 解决 |
|------|------|------|
| `JSONDecodeError` | content JSON 含中文弯引号 `””` 或混入半角引号 | 用 Python json.dump 重建，禁止手写 JSON 字符串 |
| `未知板块` | sources[].used_in 中的 id 与 sections[].id 不匹配 | 检查 used_in 数组中板块名是否与 sections id 完全一致 |
| `qdii_date_label KeyError` | 缺少 qdii_date_label 字段 | 添加该字段，格式参考 template |
| `行情快照已过期` | market_snapshot.json 超过 2 小时 | 重新运行 fetch_market.py |
| `本周财经日历为空` | 没跑 fetch_calendar.py 或日历源全挂 | 重新运行 fetch_calendar.py，确认 `calendar_feed.json` 有 events |
| `日期星期错配` / `早于简报日期` | 手改了 calendar 日期 | 照抄 calendar_feed.json，不要手写日期 |
| `calendar_feed.json 已过期` | 距抓取超过 12 小时 | 重新运行 fetch_calendar.py |
| `content.as_of 与行情快照不匹配` | as_of 与 market_snapshot.run_started_at 不一致 | 原样复制 run_started_at |
| `正文字符数超限` | 超 7000 字 | 看分段统计，找最大的 2-3 个板块各砍半条 bullet |
| `正文字符数偏少` | 不足 6000 字 | 优先在 ai_tech 或 focus 加一条完整 bullet |

## 效率核心原则

1. **并行优先**：Phase 1（fetch_market + fetch_views + fetch_news + fetch_calendar）并行；Phase 2（≤5组核验搜索）一次并发；不批量抓原文
2. **一次生成 JSON**：从 template 复制后用 Python json.dump 生成，不手写字符串不逐行调试
3. **一次砍字数**：首次构建看分段统计 → 找最大板块按比例砍 → 二次构建验证
4. **不在JSON格式上浪费回合**：中文引号是最大的沉默杀手，永远用 json.dump
5. **不重复搜索同一主题**：每组关键词搜一次，不在同一主题上换词

## 数据与日期边界

为避免多个项目共用 `/tmp` 导致旧行情串入，行情快照固定保存在本目录；QDII 缓存采用云端优先、项目内缓存兜底。周末和休市日仍使用当天的内容与草稿文件名；开市标的取本次最新报价，休市标的由本次快照给出最近交易值及休市状态，不得沿用旧快照或旧日期。

## 财经日历数据源

- `fetch_calendar.py` 是「本周财经日历」的唯一写入入口，输出 `calendar_feed.json`（默认窗口 `as_of` 起 7 个自然日，最多 5 条）。
- 主源：**金十数据日历**（jin10 远程 MCP `list_calendar`，本自然周经济数据 + 1-5 星重要性），需 `JIN10_MCP_TOKEN`。
- 兜底与跨周补充：**东方财富财经日历**（`RPT_CPH_FECALENDAR`，无需鉴权），提供未来 7 天的会议/发布会等事件，并在金十不可用时替代经济数据。
- 筛选口径：剔除国债竞拍、周度库存分项、ISM 分项等管道类噪音；按「重要性星级 + 地区权重（美国 > 欧元区/中国 > 其他）+ 关键指标标签」打分；同一时刻同地区的数据合并为一行（如非农、失业率、时薪）；不足 3 条时逐级放宽阈值。
- 事件文案自带北京时间前缀（如 `20:30 美国8月非农就业人数、失业率`），日期列由脚本按真实日历生成星期，不由模型书写。

## 行情数据源

- `fetch_market.py` 是行情唯一写入入口，不依赖 yfinance；除 VIX 外不使用 Yahoo。VIX 为 Yahoo chart 优先、CBOE 兜底（服务器 IP 常被 Yahoo 429，届时自动走 CBOE）；当前新浪、东方财富、Yahoo 和 CBOE 的逐项映射及新鲜度口径见 `README.md`「行情口径」。
- 东方财富请求必须使用 `push2delay` 延时主机，禁止换成易限流空响应的 `push2`；东财失败时仅允许退回脚本内置的新浪兜底代码（`hf_OIL` / `globalbd_us10yt` / `DINIW`）；任何核心标的缺失或错位都应硬失败，不得从新闻或旧快照补值。

## 生产调度（服务器 DeepSeek 写稿 + 发布）

- **当前生产（2026-10-09 起）**：唯一生产任务在腾讯云服务器 `115.159.56.153`（微信 API IP 白名单所在），北京时间每日 08:20 由 cron 触发 `run_brief.sh`：Phase 1 采集 → `write_brief.py` 调 DeepSeek 写稿（结构/字数/强调色定向修复 + 确定性兜底）→ `build_brief.py` 构建与终检 → `publish_wechat.py` 建草稿 + 飞书通知。已获用户授权的每日自动任务可直接发布当天草稿；失败时不得重复创建。本机调度保持停用，禁止形成两个发布者。
- 服务器 crontab 只保留这一行（`CRON_TZ=Asia/Shanghai`）：`20 8 * * * mkdir -p /home/ubuntu/finance-brief/logs && /usr/bin/flock -n /tmp/finance-brief.lock /home/ubuntu/finance-brief/run_brief.sh >> /home/ubuntu/finance-brief/logs/cron.log 2>&1`。`server_publish.sh` 的 cron 已移除。
- 服务器 `.secrets.env`（chmod 600）必填 `DEEPSEEK_API_KEY`、`BRIEF_GITHUB_TOKEN`（同步代码用，只读）；`JIN10_MCP_TOKEN`、`LARK_APP_ID`、`LARK_APP_SECRET` 按数据源需要选配。cron 重定向前必须先创建 `logs/`。
- `fetch_views.py` 失败属于非致命告警；行情、新闻或财经日历失败（`fetch_calendar.py` 重试一次仍失败）、结构/字数校验失败、构建失败均必须终止。DeepSeek 自动稿没有实时 WebSearch，发布前建议复核关键数字。
- **云端写稿通道（已暂停，保留待复用）**：2026-09-27 至 10-08 曾由 Claude Code cloud Routine 写稿、经 GitHub `briefs` 分支交接、服务器 `server_publish.sh` 发布。用户已于 10-09 主动停用 Routine，相关代码（`cloud_collect.sh`、`cloud_handoff.py`、`make_news_pool.py`、`server_publish.sh`）保留但不运行。恢复步骤见 [`docs/cloud-routine.md`](docs/cloud-routine.md)；恢复时必须先注释掉服务器的 `run_brief.sh` cron，同一天只能有一个发布者。

## 代码同步

- 代码以 GitHub `main` 为唯一权威版本：改动在本机分支上完成并测试 → 推送分支、开 PR → 用户合并到 `main` → 本机 `git pull --ff-only origin main` → 服务器同步。禁止直接在服务器或本机 main 上形成长期分叉。
- 服务器同步：`cd /home/ubuntu/finance-brief && python3 server_sync.py`，经 GitHub REST API 把 `main` 的跟踪文件同步过来（服务器连不上 github.com，但 api.github.com 可达），运行数据不动，被覆盖的旧文件备份到 `backups/sync_*/`，同步到的提交记在 `.synced_commit`。可先加 `--dry-run` 预览。
- **禁止**再用 `git push production main`：该方式会用本机 main 整体 `checkout -f` 覆盖服务器，2026-10-10 曾因本机 main 落后 GitHub 而把服务器代码回退。本机已移除 `production` remote。
