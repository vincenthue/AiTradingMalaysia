# AiTradingMalaysia

马股 + 美股**模拟交易**系统｜**只用虚拟资金**，没有接任何真实券商，也不会真实下单。

| 市场 | 虚拟本金 | 指数 | 每手 | 自动运行（马来西亚时间） |
|---|---|---|---|---|
| 🇲🇾 马股 MY（Bursa Malaysia） | RM 1,000 | FBM KLCI `^KLSE` | 100 股 | 周一至五 18:37（后备 19:47、21:17） |
| 🇺🇸 美股 US | USD 200 | S&P 500 `^GSPC` | 1 股 | 周二至六 06:37（后备 07:47、09:17；= 纽约前一天收市后） |

- 每个市场**完全独立**：自己的账户、时区、"今天"、运行记录；一个市场出错不影响另一个；一个市场的现金不能买另一个市场的股票
- 策略：50MA / 200MA 交叉（只在交叉当天产生信号），用收盘价判断，**下一个交易日开盘价**成交
- 风控：整手买卖、不做空、8% 止损、最多 5 只；单只上限、最低现金保留按市场设定（见 `config/markets.yaml`）
- 费用按市场计算：马股 = 佣金 max(0.1%, RM 8) + 清算费 0.03% + 印花税（每 RM 1000 收 RM 1，上限 RM 1000）；美股 = 佣金 max(0.1%, $1) + SEC fee（只在卖出时收）
- 结果写进 Supabase、发 Telegram（讯息开头标明 🇲🇾 [MY] / 🇺🇸 [US]），Dashboard 在 Vercel（总览 / 马股 / 美股）
- 原则：**宁可不交易，也不要错误交易**。数据或状态有问题时，该市场停止交易、发警报、退出

> ⚠️ 仅供学习研究，不构成投资建议。

---

## 密钥规则（最重要）

所有 token / key **只**放在：

| 位置 | 用途 |
|---|---|
| `.env`（本地，已被 `.gitignore` 排除） | 本地运行 bot |
| GitHub **Settings → Secrets and variables → Actions** | 云端运行 bot |
| `dashboard/.env.local`（本地，已排除） / Vercel **Environment Variables** | Dashboard（只用 anon / publishable key） |

**绝对不要**把任何 token / key 写进代码、md 文件或任何会被 commit 的文件。`.env.example` 只列变量名称。
repo 是 public 的，每次 push 都会跑 gitleaks 扫描密钥。所有市场共用同一组密钥。

---

## 设置步骤

### 1. Telegram

Telegram bot 已经建好、`chat_id` 已知。只需要在 `.env`（本地）和 GitHub Secrets（云端）填这两个变量：

| 变量 | 值 |
|---|---|
| `TELEGRAM_BOT_TOKEN` | @BotFather 给的 token（格式像 `123456789:AA...`）。**不要贴给任何人** |
| `TELEGRAM_CHAT_ID` | 你的 chat id（纯数字） |

本地：复制 `.env.example` 为 `.env`，把值填在等号后面。

<details>
<summary>以后要重新建 bot / 重新找 chat_id 的话</summary>

1. Telegram 搜索 **@BotFather**，发送 `/newbot`，按提示取名字和 username（必须以 `bot` 结尾），拿到 token。
2. 搜索你的 bot，点 **Start**。
3. 在自己电脑的浏览器打开 `https://api.telegram.org/bot<你的token>/getUpdates`，找 `"chat":{"id":123456789` —— 这串数字就是 chat id。结果是空的就先给 bot 发一句话再重新整理。
</details>

### 2. Supabase

1. 打开 <https://supabase.com>，点 **Start your project**，用 GitHub 登入。
2. 建 Organization 时选 **Free** 方案。然后点 **New project**：
   - **Project name**：`AiTradingMalaysia`
   - **Database Password**：点 **Generate a password**（bot 用不到，但请存好）
   - **Region**：选 **Southeast Asia (Singapore)**
   - 点 **Create new project**，等 1～2 分钟
3. 左边菜单点 **SQL Editor** → **New query**，把本 repo 的 `sql/schema.sql` 全部内容贴进去，点 **Run**。
   看到 *Success. No rows returned* 就对了（如果弹出 RLS 提示，选 **Run and enable RLS**）。可以重复执行，不会删资料。
4. 左边菜单点 **Table Editor**，应该看到 7 张表：`accounts`、`positions`、`trades`、`pending_orders`、`daily_snapshots`、`bot_runs`、`latest_prices`。
   每张表都有 `market` 栏位；各市场的账户会在 bot 第一次跑该市场时自动建立。
5. 拿三个值（左下 **Project Settings**）：
   - **Data API**（或 **API**）→ **Project URL** → 这是 `SUPABASE_URL`
   - **API Keys** → **Publishable key**（旧版叫 `anon` `public`）→ 给 Dashboard 用，可以公开
   - **API Keys** → **Secret keys**（旧版叫 `service_role`）→ 这是 `SUPABASE_SERVICE_KEY`，**只能**给 bot 用，**绝对不能**放进 Dashboard 或公开

> 安全设计：所有表都开了 RLS，anon / publishable key 只能 **读**；写入只能用 service key，交易通过数据库函数原子执行，而且只动该市场的数据。

<details>
<summary>已经在用旧版（只有马股）的数据库：按顺序跑迁移，不要跑 schema.sql</summary>

在 SQL Editor 依序执行 `sql/migrations/` 里的文件（每个都是一个 transaction，出错就全部不生效）：

1. `000_backup_20261126.sql` —— 先在数据库内备份（另外建议在 **Table Editor** 每张表的 **⋯ → Export data → Export table as CSV** 下载一份）
2. `001_multi_market_expand.sql` —— 加 `market` 栏位，现有数据全部 = `MY`（旧版 bot 照常能跑）
3. 新版 bot 合并上线后：`002_multi_market_unique.sql` —— 唯一约束改成按市场 + 建立美股账户
4. 新版 dashboard 上线后：`003_multi_market_cleanup.sql` —— 删除兼容用的东西，结构和 `schema.sql` 完全一样

`001_rollback.sql` 可以在还没有美股数据前退回 001。
</details>

### 3. 本地运行（可选，但建议先做）

```powershell
py -3.13 -m venv .venv
.venv\Scripts\python -m pip install -r requirements.txt
.venv\Scripts\python -m pytest -q
.venv\Scripts\python -m bot.main --market MY --dry-run
.venv\Scripts\python -m bot.main --market US --dry-run
```

- `--market` 必填（`config/markets.yaml` 里的市场代码）。
- `--dry-run`：抓真实股价，但用内存数据库、只把讯息印在终端，**不写数据库、不发 Telegram**。
- 想在本地真正跑一次：`.env` 填好 4 个值，再执行 `.venv\Scripts\python -m bot.main --market MY`。
  该市场收市前运行会被记录为 skipped（马股 17:30 马来西亚时间、美股 16:30 纽约时间之前），避免用盘中价格交易。

### 4. GitHub repo

1. 打开 <https://github.com/new>：
   - **Repository name**：`AiTradingMalaysia`
   - 选 **Public**（Actions 免费额度不限）
   - **不要**勾 *Add a README*（本地已有）
   - 点 **Create repository**
2. 在本地 push：

```powershell
git remote add origin https://github.com/<你的帐号>/AiTradingMalaysia.git
git push -u origin main
```

### 5. GitHub Secrets

进 repo → **Settings** → 左边 **Secrets and variables** → **Actions** → **New repository secret**，逐个加入（两个市场共用）：

| Name | Value |
|---|---|
| `SUPABASE_URL` | Supabase Project URL |
| `SUPABASE_SERVICE_KEY` | Supabase **Secret key**（service_role） |
| `TELEGRAM_BOT_TOKEN` | BotFather 给的 token |
| `TELEGRAM_CHAT_ID` | 你的 chat id |

### 6. 启用并测试两个交易 workflow

1. 进 repo → **Actions**。如果出现 *Workflows aren't being run on this repository*，点 **I understand my workflows, go ahead and enable them**。
2. 左边点 **Trading Bot MY** → 右边 **Run workflow** → **Branch: main** → **Run workflow**。
3. 左边点 **Trading Bot US** → 同样 **Run workflow**。
4. 等 1～2 分钟，两个都是绿色 ✅。Telegram 会收到（每则开头标明市场）：
   - 收市后：📊 每日总结
   - 收市前：⏰ 还没收市，这次不交易（正常）
   - 当天该市场已经成功跑过：⏭️ 今天已经跑过（不会重复交易）

本 repo 的 workflow：

| Workflow | 什么时候跑（cron 是 UTC） | 做什么 |
|---|---|---|
| **Trading Bot MY** | `37 10`、`47 11`、`17 13 * * 1-5` = 马来西亚 18:37 / 19:47 / 21:17（Bursa 17:00 收市后） | `python -m bot.main --market MY` |
| **Trading Bot US** | `37 22`、`47 23 * * 1-5`、`17 1 * * 2-6` = 纽约收市后，马来西亚隔天 06:37 / 07:47 / 09:17 | `python -m bot.main --market US` |
| **Tests** | 每次 push / PR | pytest + gitleaks 密钥扫描 |
| **Keepalive** | 每周日 10:00 MYT | ping Supabase 防止暂停，重新启用两个交易 workflow |

**为什么一天排三次**：GitHub 的定时任务不保证准时（忙的时候会延迟、甚至跳过一次）。所以每个市场排一次主要时间 + 两次后备。
后备运行（以及主要那次）用 `--quiet-repeat`：该市场今天已经成功 → 安静跳过（不发讯息、不重复交易）；
主要那次失败或数据还没更新 → 后备会自动再试；休市通知每天只发一次。手动 **Run workflow** 不加这个参数，重跑时照常发 ⏭️。
**自动补跑错过的交易日**：每次运行都会先检查该市场上次成功之后、今天之前，有没有还没处理的工作日（最多往回 7 天），
有就按顺序逐日补上（每天只用那天收市为止的数据，不偷看答案），再处理今天。所以就算 GitHub 晚了好几个小时、过了午夜才启动，
错过的那天也会补上，并收到那天的 📊；补跑的某天失败会停下来，下次再试。

两个交易 workflow 各自有 `concurrency`（`trading-bot-MY`、`trading-bot-US`）：同一市场同时只能有一个在跑，两个市场可以同时跑。
运行成功后会 commit `logs/last_run.json`（按市场分段记录），保持 repo 活跃。

### 7. Vercel Dashboard

1. 打开 <https://vercel.com>，点 **Sign Up**（选 **Hobby**），用 **Continue with GitHub** 登入。
2. **Add New...** → **Project** → 在 **Import Git Repository** 找到 `AiTradingMalaysia` → **Import**。
   找不到就点 **Adjust GitHub App Permissions** 授权这个 repo。
3. 在 **Configure Project**：
   - **Root Directory** → **Edit** → 选 `dashboard` → **Continue**（⚠️ 最重要）
   - **Framework Preset**：**Vite**
   - 展开 **Environment Variables**，加入：

     | Key | Value |
     |---|---|
     | `VITE_SUPABASE_URL` | Supabase Project URL |
     | `VITE_SUPABASE_ANON_KEY` | Supabase **Publishable key**（anon）——**绝对不是** secret key |

4. 点 **Deploy**。完成后点 **Visit** 打开网站。之后每次 push 到 `main`，Vercel 会自动重新部署；
   push 到其他分支会产生 **Preview** 网址（Vercel → 项目 → **Deployments**），可以先检查再合并。

Dashboard 顶部可以切换 **总览 / 马股 / 美股**（网址 `#/overview`、`#/MY`、`#/US` 可以直接收藏）：

- **总览**：每个市场一张卡片（各自货币的总资产、总收益率、对比指数、最近一次运行），加上所有市场的收益率曲线（用 %，不换算汇率）
- **单一市场**：指标卡、收益曲线（bot vs 指数）、持仓、待执行挂单、交易记录（可按日期和代码筛选）、最近运行状态

本地预览 Dashboard：复制 `dashboard/.env.example` 为 `dashboard/.env.local` 填入上面两个值，然后：

```powershell
npm --prefix dashboard install
npm --prefix dashboard run dev
```

---

## 回测（只在本地跑）

用过去几年的真实股价，假装 bot 当时每天都在跑，看策略的成绩，和同期「买入持有指数」比较：

```powershell
.venv\Scripts\python -m scripts.backtest --market MY --open
.venv\Scripts\python -m scripts.backtest --market US --open
```

- 默认过去 **10 年**；`--years 5`，或 `--start 2018-01-01 --end 2022-12-31` 指定期间。
- 想试别的均线参数（不改 `markets.yaml`）：`--short 20 --long 50`。
- 每天调用的是**和正式 bot 完全一样**的流程：策略、风控、费用、数据验证、止损都一样，信号用当天收盘价、**下一个交易日开盘价**成交（不偷看答案）。
- 只用内存数据库：**不写 Supabase、不发 Telegram**，不影响每天在跑的 bot。
- 终端会印出摘要（总收益率、年化、最大回撤、交易次数、胜率、费用）；HTML 报告（收益曲线 vs 指数、每一笔交易）放在 `reports/`（不会被 commit），`--open` 会用浏览器打开。
- ⚠️ 回测是模拟：过去的成绩不代表未来。观察名单是今天挑的股票，回测过去会有幸存者偏差；股价未复权、不含股息。

---

## 如何加一个新市场（例如新加坡）

**不用改任何 Python 代码，也不用改数据库**，只要两步：

1. 在 `config/markets.yaml` 的 `markets:` 下面加一个区块（照抄 `MY` 或 `US` 再修改）：

   ```yaml
     SG:
       name: Singapore Exchange
       flag: "🇸🇬"
       timezone: Asia/Singapore
       timezone_label: 新加坡时间
       market_close_cutoff: "17:30"   # 当地时间，收市后才运行
       currency: SGD
       currency_symbol: "S$"
       initial_cash: 1000
       benchmark: "^STI"
       lot_size: 100
       watchlist:
         - D05.SI
         - O39.SI
       fees:                          # 只写这个市场要收的费用项目
         brokerage_pct: 0.001
         brokerage_min: 10
         clearing_pct: 0.000325
       # risk: / strategy:  只写要覆盖 defaults 的项目
   ```

   费用可以用的项目：`brokerage_pct`、`brokerage_min`、`clearing_pct`、`stamp_duty_per_1000`、`stamp_duty_max`、`sec_fee_pct_on_sell`（写错名称会直接报错）。
   如果新市场有一种全新的费用，才需要在 `bot/fees.py` 的 `FEE_COMPONENTS` 加一个项目。

2. 复制 `.github/workflows/trading-bot-my.yml` 为 `trading-bot-sg.yml`，改三个地方：
   `name: Trading Bot SG`、三个 `cron`（换算成该市场收市后的 UTC 时间：一次主要 + 两次后备）、`group: trading-bot-SG`、`MARKET: SG`。
   再把 `trading-bot-sg.yml` 加进 `keepalive.yml` 的重新启用清单。

然后：

```powershell
.venv\Scripts\python -m bot.main --market SG --dry-run
```

确认 ticker 都抓得到、没有警报后 commit + push，在 **Actions** 对新 workflow 按 **Run workflow** 测试一次。
bot 第一次正式跑会自动在 `accounts` 建立该市场的账户；Dashboard 也会自动多一个分页
（想显示中文名称 / 国旗 / 指数名称，可以在 `dashboard/src/markets.js` 加一行，不加也能显示）。

---

## 疑难排解

### 没收到 Telegram

1. 去 GitHub **Actions** 看最近一次 **Trading Bot MY** / **Trading Bot US** 是否成功（绿色 ✅）。红色 ❌ 就点进去看 log。
2. 确认你有给 bot 发过 `/start`（bot 不能主动找没聊过天的人）。
3. 确认 Secrets 里 `TELEGRAM_CHAT_ID` 是纯数字、`TELEGRAM_BOT_TOKEN` 完整无空格。
   改 Secret：**Settings → Secrets and variables → Actions** → 点该 secret 旁的 ✏️ **Update**。
4. 用浏览器打开 `https://api.telegram.org/bot<你的token>/getMe`，回 `"ok":true` 才表示 token 有效。
   token 外泄或失效：到 @BotFather 发 `/revoke` 换新 token，再更新 Secret。

### Supabase 被暂停

免费项目一周没活动会被暂停（每日 bot 和每周 **Keepalive** 都会读写数据库来避免）。如果还是被暂停，警报会写「数据库连不上，可能被暂停」：

1. 登入 Supabase，项目会显示 *Paused*，点 **Resume project**（或 **Restore project**），等几分钟。
2. 回 GitHub **Actions** → 对受影响的市场按 **Run workflow** 补跑当天。
   （失败或跳过的日期可以重跑，会被覆盖；已经成功的日期不会重复交易。）

### Workflow 被停用

GitHub 在 public repo **60 天没有 commit** 时会自动停用 scheduled workflow。bot 每天 commit `logs/last_run.json`，Keepalive 也会重新启用，一般不会发生。如果发生：

1. **Actions** → 左边点 **Trading Bot MY**（和 **Trading Bot US**）→ 上方黄色提示条点 **Enable workflow**。
2. 也检查 **Keepalive** 是否被停用，同样点 **Enable workflow**。
3. **Run workflow** 手动跑一次确认正常。

### yfinance 抓不到数据

bot 会发 ⚠️ `[AiTradingMalaysia][MY]`（或 `[US]`）警报，**该市场不交易**，另一个市场不受影响。常见原因：

| 现象 | 原因 / 处理 |
|---|---|
| 📅 今天休市，没有新数据 | 该市场公共假期（按该市场自己的日期判断），正常，不用处理 |
| 马股**平常交易日**却收到 📅，或傍晚收到「价格是 NaN / 空值」⚠️ | Yahoo 有时收市后一段时间才更新完当天的马股收盘数据。当天先不交易（安全）；19:47 和 21:17 的后备运行会自动再试。到 21:17 之后还是没有 📊，就到 **Actions → Trading Bot MY → Run workflow** 补跑 |
| 某只股票 empty / no data | ticker 写错（马股要带 `.KL`，例如 `7113.KL`；美股不用后缀），或已下市；修改 `config/markets.yaml` 的 `watchlist` |
| 全部股票都失败 | Yahoo Finance 暂时故障或限流；等几小时后 **Run workflow** 重跑 |
| 数据过期（stale） | Yahoo 还没更新收盘价，或 yfinance 坏了；稍后重跑，持续发生就升级 yfinance |
| 单日涨跌超过 30% 被标记可疑 | 可能是数据错误或除权，该股票当天不交易；其他股票照常 |

若长期失败，先在本地跑 `.venv\Scripts\python -m bot.main --market MY --dry-run` 看详细错误，或升级 yfinance：
`.venv\Scripts\python -m pip install -U yfinance`，测试通过后更新 `requirements.txt`。

### 两个 workflow 同时 push 冲突

马股和美股的 workflow 都会 commit `logs/last_run.json`。每次 push 前会先拿最新的 `main`，**只把自己市场那一段**写回去（`scripts/merge_last_run.py`），失败最多重试 5 次，所以一般不会冲突。

如果 log 出现「push 重试 5 次都失败」：

1. 交易本身**已经完成**（数据库和 Telegram 都正常），只是 `last_run.json` 没更新，不影响交易。
2. 等几分钟再对该市场按 **Run workflow**：当天已经成功 → 只会收到 ⏭️，并补上 `last_run.json`。
3. 持续失败的话，检查 workflow 的 `permissions: contents: write` 还在，以及 **Settings → Actions → General → Workflow permissions** 是 **Read and write permissions**。

---

## 项目结构

```
bot/                交易逻辑
  main.py             入口：python -m bot.main --market MY（或 US）
  config.py           读取 markets.yaml（defaults + 市场覆盖）和环境变量
  market.py           市场时区、"今天"、收市判断、货币格式
  data_fetcher.py     抓股价 + 数据验证 + 重试
  strategy.py         50MA / 200MA 交叉信号
  fees.py             按项目计算费用（每个市场只列自己要收的）
  risk.py             风控
  broker.py           模拟券商（下一个交易日开盘价成交）
  db.py               Supabase / 内存数据库（每个实例只属于一个市场）
  notifier.py         Telegram 讯息
  safety.py           幂等、状态检查、密钥遮蔽
config/markets.yaml 每个市场的观察名单、费用、风控（不含密钥）
sql/schema.sql      全新安装用的完整结构
sql/migrations/     旧版（只有马股）升级到多市场用
scripts/            backtest.py（回测）、keepalive.py、merge_last_run.py
tests/              pytest（假数据，不连网络和数据库）
dashboard/          Vue 3 + Vite + ECharts（总览 / 马股 / 美股）
.github/workflows/  trading-bot-my.yml、trading-bot-us.yml、tests.yml、keepalive.yml
logs/last_run.json  每个市场最近一次运行结果（bot 自动更新）
```
