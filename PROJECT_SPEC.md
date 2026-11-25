# AiTradingMalaysia：马股模拟交易系统（项目说明）

> 给 Claude Code 的说明：请完整阅读本文件后再开始写代码。按「开发阶段」一个阶段一个阶段完成，每个阶段结束时跑测试，并向我汇报做了什么、还需要我手动做什么。遇到本文件没写清楚的地方，先问我，不要自己假设。

---

## 0. 最高原则（不可违反）

1. **只用虚拟钱。** 系统里不准出现任何真实券商 API、真实下单、真实资金相关的代码。所有“买卖”都只是数据库里的记录。
2. **任何密钥都不能写进代码或 commit 进 repo。** repo 是 public 的。所有 token 和 key 只能从环境变量读取（本地用 `.env`，云端用 GitHub Secrets）。`.env` 必须在 `.gitignore` 里。
3. **宁可不交易，也不要错误交易。** 数据有问题、状态不确定、发生异常时，bot 一律停止交易，发 Telegram 警报，然后退出。

---

## 1. 项目目标

做一个零成本、全自动运行的马来西亚股票（Bursa Malaysia）模拟交易系统：

- 每个交易日收市后自动运行，不需要我的电脑开着
- 按设定的策略，用虚拟资金（初始 RM 10,000）自动买卖
- 每次运行后发 Telegram 通知（交易内容和每日总结）
- 有一个网页 dashboard 显示持仓、交易记录、收益曲线，并和“直接持有指数”比较
- 出问题时能自动预防、自动恢复，或者至少马上通知我

---

## 2. 技术栈

| 部分 | 技术 | 费用 |
|---|---|---|
| Bot 主程序 | Python 3.12 | 免费 |
| 定时运行 | GitHub Actions（public repo，cron 定时任务） | 免费 |
| 数据库 | Supabase 免费版（PostgreSQL） | 免费 |
| 股价数据 | `yfinance`（马股代码后缀 `.KL`） | 免费（非官方） |
| 通知 | Telegram Bot API | 免费 |
| 前端 | Vue 3 + Vite，部署在 Vercel | 免费 |
| 测试 | `pytest` | 免费 |

所有 Python 依赖都要在 `requirements.txt` 里**锁定版本**，避免哪天库更新后程序突然坏掉。

---

## 3. 项目结构

项目名称是 **AiTradingMalaysia**。项目根目录（也就是我已经建好的文件夹）叫 `AiTradingMalaysia`，GitHub repo 也用这个名字。代码里需要用到项目名称的地方（README 标题、`package.json` 的 name、Telegram 讯息前缀等）也统一用 AiTradingMalaysia。直接在现有的根目录里建文件，**不要**再建一层子文件夹。

```
AiTradingMalaysia/
├── bot/
│   ├── main.py              # 入口：一次完整运行的流程
│   ├── config.py            # 读取配置和环境变量
│   ├── data_fetcher.py      # 抓股价 + 数据验证
│   ├── strategy.py          # 策略：产生买卖信号
│   ├── broker.py            # 模拟券商：执行虚拟买卖、计算费用
│   ├── risk.py              # 风控规则
│   ├── db.py                # Supabase 读写
│   ├── notifier.py          # Telegram 通知
│   └── safety.py            # 锁、幂等检查、健康检查
├── config/
│   └── settings.yaml        # 股票清单、策略参数、费用、风控参数（不含密钥）
├── sql/
│   └── schema.sql           # 建表 SQL + RLS 政策
├── tests/                   # pytest 测试
├── scripts/
│   ├── backtest.py          # 回测（第 5 阶段）
│   └── keepalive.py         # 保活
├── dashboard/               # Vue 3 前端
├── logs/
│   └── last_run.json        # 每次运行后更新（用来保持 repo 活跃）
├── .github/workflows/
│   ├── trading-bot.yml      # 每日交易
│   ├── tests.yml            # push 时跑测试
│   └── keepalive.yml        # 每周保活
├── .env.example             # 列出需要的环境变量名称（不含真实值）
├── .gitignore
├── requirements.txt
└── README.md                # 设置步骤说明
```

---

## 4. 配置（`config/settings.yaml`）

```yaml
initial_cash: 10000          # RM
timezone: Asia/Kuala_Lumpur
benchmark: "^KLSE"           # FBM KLCI 指数，用来比较收益

watchlist:                   # 先用几只大型蓝筹股
  - 1155.KL   # Maybank
  - 1295.KL   # Public Bank
  - 1023.KL   # CIMB
  - 5347.KL   # Tenaga Nasional
  - 6888.KL   # Axiata

strategy:
  name: ma_crossover
  short_window: 50
  long_window: 200

risk:
  max_position_pct: 0.20     # 单只股票最多占总资产 20%
  max_positions: 5
  stop_loss_pct: 0.08        # 从买入价跌 8% 就止损卖出
  min_cash_reserve: 500      # 至少保留 RM 500 现金

fees:                        # 近似值，可调整；运行前请核对实际费率
  brokerage_pct: 0.001
  brokerage_min: 8.0
  clearing_pct: 0.0003
  stamp_duty_per_1000: 1.0
  stamp_duty_max: 1000

lot_size: 100                # Bursa 每手 100 股，只能整手买卖
```

`ticker` 代码请在开发时用 `yfinance` 实际验证一次，抓不到的要报错提醒我，不要默默跳过。

---

## 5. 数据库设计（`sql/schema.sql`）

至少要有这些表：

- **`account`**：现金余额、初始资金、更新时间（只有一行）
- **`positions`**：股票代码、持股数量、平均买入价、买入日期
- **`trades`**：每笔交易（日期、代码、BUY/SELL、数量、价格、费用、原因）
- **`pending_orders`**：已产生信号、等待下一个交易日开盘执行的订单
- **`daily_snapshots`**：每天的总资产、现金、持仓市值、指数收盘价（用来画收益曲线）
- **`bot_runs`**：每次运行记录（`run_date`、状态 `success`/`skipped`/`failed`、讯息、开始和结束时间）。`run_date` 要有 **UNIQUE 约束**。

**RLS（Row Level Security）**：所有表都要开 RLS。`anon` 角色**只能 SELECT**；写入只能用 `service_role` key（只给 bot 用，只放在 GitHub Secrets）。前端只用 `anon` key。

---

## 6. 每次运行的流程（`bot/main.py`）

```
1. 读取配置、检查所有环境变量是否齐全 → 缺任何一个就警报并退出
2. 幂等检查：今天（马来西亚时间）是否已经成功跑过？是 → 记录 skipped，退出
3. 抓取 watchlist + 指数的最新数据（含重试）
4. 数据验证（见第 7 节）→ 不通过就警报并退出，不交易
5. 交易日检查：最新数据的日期不是今天 → 视为休市，记录 skipped，发简短通知，退出
6. 执行 pending_orders（昨天产生的信号，用今天的开盘价成交）
7. 检查止损：持仓是否跌破止损线 → 产生卖出信号
8. 跑策略，产生新的买卖信号 → 经过风控检查 → 写入 pending_orders（明天开盘执行）
9. 记录 daily_snapshot
10. 记录 bot_runs = success，更新 logs/last_run.json
11. 发 Telegram 每日总结
```

**为什么用“明天开盘价成交”：** 信号是用今天的收盘价算出来的，如果又用同一个收盘价成交，等于偷看了答案（look-ahead bias）。延到下一个交易日的开盘价成交比较真实。

**数据库操作要用 transaction**：一笔交易的“扣现金、加持仓、写交易记录、删除 pending order”必须一起成功或一起失败，不能只做一半。

---

## 7. 预防事故：自动防护机制（重要）

请全部实现，并且每一项都要有对应的测试。

### 7.1 数据问题
- **抓取失败**：每个 ticker 最多重试 3 次，间隔用指数退避（例如 5、15、45 秒）。还是失败 → 整次运行停止交易，发警报。
- **数据验证**：价格不能是 NaN、0 或负数；成交量不能是负数；单日涨跌超过 30% 视为可疑，跳过该股并警报；数据不足 `long_window` 天时，该股不产生信号。
- **数据过期**：最新数据日期太旧（超过 5 天）→ 可能是 yfinance 坏了，发警报。
- **yfinance 库坏掉**：在警报讯息里附上错误内容，并提示“可能需要更新 yfinance 版本”。

### 7.2 重复执行和并发
- **幂等**：`bot_runs.run_date` 是 UNIQUE。同一天跑第二次（例如我手动触发）不会重复交易。
- **并发锁**：GitHub Actions workflow 设定 `concurrency`，同一时间只能有一个 bot 在跑。
- **状态一致性**：每次运行开始时检查 `account` 现金不能是负数、持仓数量是 `lot_size` 的整数倍；不一致 → 停止交易并警报。

### 7.3 交易规则防护（`risk.py`）
- 不能买到现金变负数，也不能低于 `min_cash_reserve`
- 只能整手（100 股）买卖
- 不能卖出没有持有的股票（不准做空）
- 单只股票不能超过 `max_position_pct`，持仓数不能超过 `max_positions`
- 所有费用都要计入（买卖都收）

### 7.4 平台限制
- **GitHub 60 天不活跃会停用定时任务**：每次成功运行后更新并 commit `logs/last_run.json`（workflow 需要 `permissions: contents: write`）。另外加一个每周跑的 `keepalive.yml` 作为备援。
- **Supabase 一周不活跃会暂停**：每日 bot 本身就会读写数据库。`keepalive.yml` 也顺便对 Supabase 做一次简单查询。
- **Supabase 被暂停时连不上**：发明确的警报，例如“数据库连不上，可能被暂停，请到 Supabase Dashboard 点 Resume”。

### 7.5 “沉默就是出问题”
- 每个交易日**一定**发一则总结，就算没有交易也要发（“今日无交易，总资产 RM xxx”）。如果我某天没收到讯息，就知道系统出问题了。
- 任何未捕捉的异常：最外层 `try/except` 捕捉，发警报（附错误类型和简短 traceback），记录 `bot_runs = failed`，然后让 workflow 以非零状态结束，这样 GitHub 也会寄 email 通知我。
- Telegram 发送失败本身不能让程序崩溃（重试 2 次，失败就只写 log）。

### 7.6 安全
- 所有密钥只从环境变量读取。启动时检查，缺少的话报错时**只显示变量名称，不显示值**。
- log 和 Telegram 讯息里绝对不能出现任何 token 或 key。
- 加一个 pre-commit 检查或 CI 步骤扫描是否有密钥被 commit（例如用 `gitleaks`）。

---

## 8. 定时任务（`.github/workflows/trading-bot.yml`）

- **触发时间**：Bursa 交易时间是马来西亚时间 9:00–12:30 和 14:30–17:00。在收市后运行，例如马来西亚时间 18:30，也就是 UTC 10:30，周一到周五：`cron: "30 10 * * 1-5"`
- 同时开启 `workflow_dispatch`，让我可以手动触发测试
- 设定 `concurrency: { group: trading-bot, cancel-in-progress: false }`
- 设定 `timeout-minutes: 15`，避免卡住
- 缓存 pip 依赖，加快运行
- 公共假期不需要另外维护日历：第 6 节的“最新数据日期不是今天 → 休市”已经能处理

---

## 9. Telegram 通知格式（`notifier.py`）

- **交易通知**：`🟢 BUY 300 × 5347.KL @ RM 13.20｜原因：50MA 上穿 200MA`
- **每日总结**：总资产、今日变化、总收益率、同期指数收益率、现金、持仓数
- **警报**：`⚠️ [AiTradingMalaysia] 错误类型 + 简短说明 + 建议怎么处理`
- **休市**：`📅 今天休市，没有新数据`

---

## 10. 前端 Dashboard（`dashboard/`）

- Vue 3 + Vite，用 `@supabase/supabase-js` 和 **anon key（只读）** 读取数据
- 页面内容：
  - 顶部指标卡：总资产、总收益率、指数同期收益率、现金
  - 收益曲线：bot vs 指数（从 `daily_snapshots`）
  - 持仓表：代码、数量、平均成本、现价、盈亏 %
  - 交易记录表（可按日期和代码筛选）
  - 运行状态：最近几次 `bot_runs`（成功、跳过、失败）
  - 页面明显位置标示「模拟交易｜虚拟资金」
- 部署到 Vercel，环境变量设定 `VITE_SUPABASE_URL` 和 `VITE_SUPABASE_ANON_KEY`

---

## 11. 测试（`tests/`）

用假数据（mock），测试时**不连真实网络和真实数据库**：

- 均线交叉信号是否正确
- 费用计算（含最低佣金、印花税上限）
- 风控：现金不足、超过仓位上限、非整手、卖出没有持有的股票 → 都要被拒绝
- 止损触发
- 数据验证：NaN、0、异常涨跌、数据过期
- 幂等：同一天跑两次只交易一次
- 休市检测
- 异常时是否发警报、是否不交易

另外加 `tests.yml` workflow，每次 push 自动跑测试。

---

## 12. 开发阶段

**第 1 阶段：核心逻辑（本地）**
项目结构、配置、data_fetcher（含验证和重试）、strategy、broker、risk，加上对应的测试。先用本地 SQLite 或内存模拟数据库，让整个流程能在本地 `--dry-run` 跑通。

**第 2 阶段：Supabase + Telegram**
schema.sql、db.py（含 transaction）、notifier.py、safety.py（幂等、状态检查）。本地连真实 Supabase 和 Telegram 测一次完整流程。

**第 3 阶段：GitHub Actions 上线**
trading-bot.yml、tests.yml、keepalive.yml、last_run.json 自动 commit、密钥扫描。用 `workflow_dispatch` 手动触发测试。

**第 4 阶段：Dashboard**
Vue 前端并部署到 Vercel。

**第 5 阶段（可选）：回测**
`scripts/backtest.py`：用过去几年的数据跑同一个策略，并和 buy-and-hold 指数比较，输出报告。

---

## 13. README.md 必须包含的设置步骤（给我手动做的部分）

请把以下步骤写成清楚的说明，包括截图位置或具体菜单名称（用英文界面名称）：

1. 在 Telegram 找 @BotFather 创建 bot，拿 token；给自己的 bot 发 `/start`，再用 `getUpdates` 拿 `chat_id`
2. 注册 Supabase，建项目，在 SQL Editor 跑 `schema.sql`，拿 Project URL、anon key、service_role key
3. 在 GitHub 建 **public** repo，push 代码
4. 在 repo 的 **Settings → Secrets and variables → Actions** 加入：`SUPABASE_URL`、`SUPABASE_SERVICE_KEY`、`TELEGRAM_BOT_TOKEN`、`TELEGRAM_CHAT_ID`
5. 在 **Actions** 页面启用 workflow，用 **Run workflow** 手动测试一次
6. 在 Vercel 导入 repo 的 `dashboard/` 目录，设定前端环境变量并部署
7. 疑难排解：没收到 Telegram、Supabase 被暂停、workflow 被停用、yfinance 抓不到数据时分别怎么办

---

## 14. 完成标准

- [ ] 本地 `python -m bot.main --dry-run` 能完整跑通，不写数据库、不发讯息
- [ ] `pytest` 全部通过
- [ ] GitHub Actions 手动触发成功，Supabase 有数据，Telegram 收到总结
- [ ] 同一天手动触发第二次，不会重复交易
- [ ] 故意给错误的 ticker 或断开数据源时，会收到警报而且没有交易
- [ ] repo 里搜索不到任何密钥
- [ ] Dashboard 能显示数据，并标示「模拟交易」
- [ ] README 设置步骤完整
