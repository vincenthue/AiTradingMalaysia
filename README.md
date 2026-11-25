# AiTradingMalaysia

马来西亚股票（Bursa Malaysia）**模拟交易**系统｜**只用虚拟资金**，没有任何真实券商或真实下单。

> 完整设置步骤（Telegram、Supabase、GitHub Secrets、Vercel、疑难排解）会在之后的阶段补上。

## 本地运行（第 1 阶段）

```powershell
py -3.13 -m venv .venv
.venv\Scripts\python -m pip install -r requirements.txt
.venv\Scripts\python -m pytest -q
.venv\Scripts\python -m bot.main --dry-run
```

`--dry-run`：抓真实股价，但使用内存数据库、只把讯息印在终端，不写数据库、不发 Telegram。

## 密钥

所有 token / key 只放在 `.env`（本地）或 GitHub Secrets（云端），**绝对不能**写进代码或 commit。
`.env.example` 只列出变量名称。
