# AiTradingMalaysia

马来西亚股票（Bursa Malaysia）**模拟交易**系统｜**只用虚拟资金（RM 1,000）**，没有接任何真实券商，也不会真实下单。

- 策略：50MA / 200MA 交叉（只在交叉当天产生信号），用收盘价判断，**下一个交易日开盘价**成交
- 风控：100 股一手、保留 RM 100 现金、单只最多 50%、最多 5 只、不做空、8% 止损
- 观察名单：8 只 RM 3 以下、成交活跃的股票（见 `config/markets.yaml`，顺序 = 买入优先顺序）
- 费用：佣金 max(0.1%, RM 8)、清算费 0.03%、印花税（每 RM 1000 收 RM 1，上限 RM 1000）
- 每个交易日 **18:30 MYT** 由 GitHub Actions 自动运行；结果写进 Supabase、发 Telegram，Dashboard 在 Vercel
- 原则：**宁可不交易，也不要错误交易**。数据或状态有问题时就停止交易、发警报、退出

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
repo 是 public 的，每次 push 都会跑 gitleaks 扫描密钥。

---

## 设置步骤

### 1. Telegram bot

1. 在 Telegram 搜索 **@BotFather**，发送 `/newbot`，按提示取名字和 username（必须以 `bot` 结尾）。
2. BotFather 会回复一串 token（格式像 `123456789:AA...`），这就是 `TELEGRAM_BOT_TOKEN`。**不要贴给任何人**。
3. 在 Telegram 搜索你刚建的 bot，点 **Start**（或发送 `/start`）。
4. 用浏览器打开 `https://api.telegram.org/bot<你的token>/getUpdates`（把 `<你的token>` 换掉，只在自己电脑上开）。
   在结果里找 `"chat":{"id":123456789` —— 这串数字就是 `TELEGRAM_CHAT_ID`。
   如果结果是空的 `"result":[]`，先再给 bot 发一句话，然后重新整理。

### 2. Supabase

1. 打开 <https://supabase.com>，点 **Start your project**，用 GitHub 登入。
2. 建 Organization 时选 **Free** 方案。然后点 **New project**：
   - **Project name**：`AiTradingMalaysia`
   - **Database Password**：点 **Generate a password**（bot 用不到，但请存好）
   - **Region**：选 **Southeast Asia (Singapore)**
   - 点 **Create new project**，等 1～2 分钟
3. 左边菜单点 **SQL Editor** → **New query**，把本 repo 的 `sql/schema.sql` 全部内容贴进去，点 **Run**。
   看到 *Success. No rows returned* 就对了。这个脚本可以重复执行，不会删资料。
4. 左边菜单点 **Table Editor**，应该看到 7 张表：`account`、`positions`、`trades`、`pending_orders`、`daily_snapshots`、`bot_runs`、`latest_prices`。
5. 拿三个值（左下 **Project Settings**）：
   - **Data API**（或 **API**）→ **Project URL** → 这是 `SUPABASE_URL`
   - **API Keys** → **Publishable key**（旧版叫 `anon` `public`）→ 给 Dashboard 用，可以公开
   - **API Keys** → **Secret keys**（旧版叫 `service_role`）→ 这是 `SUPABASE_SERVICE_KEY`，**只能**给 bot 用，**绝对不能**放进 Dashboard 或公开

> 安全设计：所有表都开了 RLS，anon / publishable key 只能 **读**；写入只能用 service key，交易通过数据库函数原子执行。

### 3. 本地运行（可选，但建议先做）

```powershell
py -3.13 -m venv .venv
.venv\Scripts\python -m pip install -r requirements.txt
.venv\Scripts\python -m pytest -q
.venv\Scripts\python -m bot.main --market MY --dry-run
```

- `--dry-run`：抓真实股价，但用内存数据库、只把讯息印在终端，**不写数据库、不发 Telegram**。
- 想在本地真正跑一次：复制 `.env.example` 为 `.env`，填入 4 个值，再执行 `.venv\Scripts\python -m bot.main`。
  注意：17:30 MYT 之前运行会被记录为 skipped（避免用盘中价格交易）。

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

进 repo → **Settings** → 左边 **Secrets and variables** → **Actions** → **New repository secret**，逐个加入：

| Name | Value |
|---|---|
| `SUPABASE_URL` | Supabase Project URL |
| `SUPABASE_SERVICE_KEY` | Supabase **Secret key**（service_role） |
| `TELEGRAM_BOT_TOKEN` | BotFather 给的 token |
| `TELEGRAM_CHAT_ID` | getUpdates 拿到的数字 |

### 6. 启用并测试 Actions

1. 进 repo → **Actions**。如果出现 *Workflows aren't being run on this repository*，点 **I understand my workflows, go ahead and enable them**。
2. 左边点 **Trading Bot** → 右边 **Run workflow** → **Run workflow**。
3. 等 1～2 分钟，绿色勾 ✅ 表示成功。你应该会收到 Telegram：
   - 17:30 MYT 之后：📊 每日总结
   - 17:30 MYT 之前：⏰ 收市前不运行（正常）
   - 当天已经成功跑过：⏭️ 今天已经跑过（不会重复交易）
4. 之后每个交易日 18:30 MYT 会自动运行（cron `30 10 * * 1-5`，UTC）。

本 repo 的 3 个 workflow：

| Workflow | 什么时候跑 | 做什么 |
|---|---|---|
| **Trading Bot** | 周一至五 18:30 MYT / 手动 | 跑 bot，更新 `logs/last_run.json` |
| **Tests** | 每次 push / PR | pytest + gitleaks 密钥扫描 |
| **Keepalive** | 每周日 10:00 MYT | ping Supabase 防止暂停，重新启用 Trading Bot workflow |

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

4. 点 **Deploy**。完成后点 **Visit** 打开网站。之后每次 push，Vercel 会自动重新部署。

本地预览 Dashboard：复制 `dashboard/.env.example` 为 `dashboard/.env.local` 填入上面两个值，然后：

```powershell
npm --prefix dashboard install
npm --prefix dashboard run dev
```

---

## 疑难排解

### 没收到 Telegram

1. 去 GitHub **Actions** 看最近一次 **Trading Bot** 是否成功（绿色 ✅）。红色 ❌ 就点进去看 log。
2. 确认你有给 bot 发过 `/start`（bot 不能主动找没聊过天的人）。
3. 确认 Secrets 里 `TELEGRAM_CHAT_ID` 是纯数字、`TELEGRAM_BOT_TOKEN` 完整无空格。
   改 Secret：**Settings → Secrets and variables → Actions** → 点该 secret 旁的 ✏️ **Update**。
4. 用浏览器打开 `https://api.telegram.org/bot<你的token>/getMe`，回 `"ok":true` 才表示 token 有效。
   token 外泄或失效：到 @BotFather 发 `/revoke` 换新 token，再更新 Secret。

### Supabase 被暂停

免费项目 7 天没活动会被暂停（本 repo 的 **Keepalive** workflow 每周 ping 一次来避免）。如果还是被暂停：

1. 登入 Supabase，项目会显示 *Paused*，点 **Resume project**（或 **Restore project**），等几分钟。
2. 回 GitHub **Actions** → **Trading Bot** → **Run workflow** 补跑当天。
   （失败或跳过的日期可以重跑，会被覆盖；已经成功的日期不会重复交易。）

### Workflow 被停用

GitHub 在 public repo **60 天没有 commit** 时会自动停用 scheduled workflow。bot 每天 commit `logs/last_run.json`，Keepalive 也会重新启用，一般不会发生。如果发生：

1. **Actions** → 左边点 **Trading Bot** → 上方黄色提示条点 **Enable workflow**。
2. 也检查 **Keepalive** 是否被停用，同样点 **Enable workflow**。
3. **Run workflow** 手动跑一次确认正常。

### yfinance 抓不到数据

bot 会发 ⚠️ 警报并**不交易**，这是设计好的保护。常见原因：

| 现象 | 原因 / 处理 |
|---|---|
| 📅 今天休市，没有新数据 | 马股公共假期，正常，不用处理 |
| 某只股票 empty / no data | ticker 写错（要带 `.KL`，例如 `1155.KL`），或已下市；修改 `config/markets.yaml` 的 `watchlist` |
| 全部股票都失败 | Yahoo Finance 暂时故障或限流；等几小时后 **Run workflow** 重跑 |
| 数据过期（stale） | Yahoo 还没更新收盘价；稍后重跑 |
| 单日涨跌超过 30% 被标记可疑 | 可能是数据错误或除权，该股票当天不交易；其他股票照常 |

若长期失败，可以先在本地跑 `.venv\Scripts\python -m bot.main --market MY --dry-run` 看详细错误，或升级 yfinance：
`.venv\Scripts\python -m pip install -U yfinance`，测试通过后更新 `requirements.txt`。

---

## 项目结构

```
bot/            交易逻辑（config、data_fetcher、strategy、risk、broker、db、notifier、safety、main）
config/         markets.yaml（每个市场的观察名单、费用、风控；defaults 共用）
sql/schema.sql  Supabase 表、RLS、交易函数
scripts/        keepalive.py
tests/          pytest
dashboard/      Vue 3 + Vite + ECharts
.github/workflows/  trading-bot.yml、tests.yml、keepalive.yml
logs/last_run.json  最近一次运行结果（bot 自动更新）
```
