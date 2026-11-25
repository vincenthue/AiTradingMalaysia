"""回测（第 5 阶段）：用过去几年的真实股价，假装 bot 当时每天都在跑，看策略的成绩。

用法：
    python -m scripts.backtest --market MY                  # 默认过去 10 年
    python -m scripts.backtest --market US --years 5
    python -m scripts.backtest --market MY --start 2018-01-01 --end 2022-12-31
    python -m scripts.backtest --market US --short 20 --long 50 --open   # 试别的均线参数，跑完打开报告

做法：每个交易日把历史数据“切到那一天为止”，然后调用和正式 bot 完全一样的 execute()：
同样的策略、风控、费用、数据验证、止损，信号用当天收盘价算、下一个交易日开盘价成交（没有偷看答案）。
只用内存数据库：不写 Supabase、不发 Telegram，不影响每天在跑的 bot。

输出：终端摘要 + reports/ 里的 HTML 报告（收益曲线 vs 指数、指标、每一笔交易）。reports/ 不会被 commit。
⚠️ 回测结果是模拟的：过去的成绩不代表未来；也不构成投资建议。
"""
from __future__ import annotations

import argparse
import html
import logging
import math
import sys
import time as _time
from dataclasses import dataclass, field, replace
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Callable, Optional
from zoneinfo import ZoneInfo

import pandas as pd

from bot import data_fetcher as dfetch
from bot.config import PROJECT_NAME, ROOT_DIR, ConfigError, Settings, load_market
from bot.db import InMemoryDB
from bot.main import execute
from bot.market import money
from bot.models import DailySnapshot, Trade

log = logging.getLogger("backtest")

REPORTS_DIR = ROOT_DIR / "reports"
DEFAULT_YEARS = 10


# ====================================================================== 数据


def warmup_days(st: Settings) -> int:
    """开始日之前要多抓多少天（日历天），让第一天就有足够数据算长均线。"""
    return int(st.strategy.long_window * 1.6) + 30


def fetch_range(st: Settings, start: date, end: date, retry_delays=(5, 15, 45),
                sleep: Callable[[float], None] = _time.sleep) -> dict[str, pd.DataFrame]:
    """抓 watchlist + 指数从 (start - 预热) 到 end 的日线（未复权，和正式 bot 一样）。

    任何一个 ticker 完全抓不到 → 抛 DataFetchError（不会默默跳过）。
    """
    import yfinance as yf

    first = start - timedelta(days=warmup_days(st))

    def downloader(ticker: str, _period: str) -> pd.DataFrame:
        return yf.Ticker(ticker).history(start=first.isoformat(), end=(end + timedelta(days=1)).isoformat(),
                                         interval="1d", auto_adjust=False)

    return dfetch.fetch_all(list(st.watchlist) + [st.benchmark], "max", downloader, retry_delays, sleep)


# ====================================================================== 回放


class _Collect:
    """收集讯息（不发出去）。"""

    def __init__(self):
        self.sent: list[str] = []

    def send(self, text: str) -> bool:
        self.sent.append(text)
        return True


@dataclass
class DayResult:
    day: date
    status: str
    message: str


@dataclass
class BacktestResult:
    settings: Settings
    start: date
    end: date
    days: list[DayResult]
    snapshots: list[DailySnapshot]
    trades: list[Trade]
    final_cash: float
    open_positions: dict
    late_starters: dict[str, date] = field(default_factory=dict)   # 开始后才有数据的 ticker → 第一天


def run_backtest(st: Settings, data: dict[str, pd.DataFrame], start: date, end: date,
                 progress: Optional[Callable[[int, int], None]] = None) -> BacktestResult:
    """在 [start, end] 每个交易日（以指数有数据的日子为准）跑一次正式 bot 的 execute()。"""
    bench = data[st.benchmark]
    calendar = [d for d in bench.index if start <= d <= end]
    if len(calendar) < 2:
        raise ValueError(f"{start}～{end} 之间 {st.benchmark} 只有 {len(calendar)} 个交易日，无法回测")

    tz = ZoneInfo(st.timezone)
    window = st.strategy.long_window + 60           # 每天只给最近这么多行：结果一样，速度快很多
    db = InMemoryDB(st.initial_cash, st.market)
    notifier = _Collect()
    first_seen = {t: data[t].index[0] for t in st.watchlist}
    late = {t: d0 for t, d0 in first_seen.items() if d0 > calendar[0]}
    days: list[DayResult] = []

    for i, day in enumerate(calendar):
        # 那一天还没上市 / 还没有数据的股票先不放进观察名单（顺序不变）
        available = tuple(t for t in st.watchlist if first_seen[t] <= day)
        day_st = replace(st, watchlist=available) if available != st.watchlist else st
        sliced = {t: data[t].loc[:day].tail(window) for t in available + (st.benchmark,)}
        now = datetime.combine(day, st.market_close_cutoff, tz) + timedelta(hours=1)
        out = execute(day_st, db, notifier, now=now,
                      downloader=lambda t, _p: sliced[t], sleep=lambda _s: None)
        days.append(DayResult(day, out.status, out.message))
        if progress:
            progress(i + 1, len(calendar))

    return BacktestResult(
        settings=st, start=calendar[0], end=calendar[-1], days=days,
        snapshots=db.get_snapshots(), trades=db.get_trades(),
        final_cash=db.get_account().cash, open_positions=db.get_positions(), late_starters=late,
    )


# ====================================================================== 指标


def max_drawdown(values: list[float]) -> float:
    """最大回撤（负数，例如 -0.23 = 从最高点最多跌了 23%）。"""
    peak, worst = -math.inf, 0.0
    for v in values:
        peak = max(peak, v)
        if peak > 0:
            worst = min(worst, v / peak - 1)
    return worst


def cagr(first: float, last: float, d0: date, d1: date) -> Optional[float]:
    years = (d1 - d0).days / 365.25
    if years <= 0 or first <= 0 or last <= 0:
        return None
    return (last / first) ** (1 / years) - 1


@dataclass
class RoundTrip:
    ticker: str
    buy_date: date
    sell_date: date
    quantity: int
    pnl: float          # 已扣买卖费用
    pnl_pct: float
    reason: str


def round_trips(trades: list[Trade]) -> list[RoundTrip]:
    """把交易配成“买进 → 卖出”，计算每次卖出的盈亏（成本含买入费用，收入扣卖出费用）。"""
    held: dict[str, dict] = {}
    out: list[RoundTrip] = []
    for t in sorted(trades, key=lambda x: (x.trade_date, x.id or 0)):
        h = held.setdefault(t.ticker, {"qty": 0, "cost": 0.0, "since": t.trade_date})
        if t.side == "BUY":
            if h["qty"] == 0:
                h["since"] = t.trade_date
            h["qty"] += t.quantity
            h["cost"] += t.quantity * t.price + t.fees
        else:
            basis = h["cost"] * t.quantity / h["qty"]
            pnl = t.quantity * t.price - t.fees - basis
            out.append(RoundTrip(t.ticker, h["since"], t.trade_date, t.quantity, round(pnl, 2),
                                 pnl / basis if basis else 0.0, t.reason))
            h["cost"] -= basis
            h["qty"] -= t.quantity
    return out


@dataclass
class Metrics:
    trading_days: int
    initial: float
    final: float
    total_return: float
    cagr: Optional[float]
    max_drawdown: float
    index_return: float
    index_cagr: Optional[float]
    index_max_drawdown: float
    buys: int
    sells: int
    fees: float
    wins: int
    closed: int
    best: Optional[RoundTrip]
    worst: Optional[RoundTrip]
    exposure: float               # 有持仓的日子占比
    failed_days: int
    skipped_days: int


def compute_metrics(r: BacktestResult) -> Metrics:
    snaps = r.snapshots
    values = [s.total_value for s in snaps]
    bench = [s.benchmark_close for s in snaps]
    initial = r.settings.initial_cash
    final = values[-1] if values else initial
    index_values = [initial * b / bench[0] for b in bench] if bench else [initial]
    trips = round_trips(r.trades)
    d0, d1 = (snaps[0].snapshot_date, snaps[-1].snapshot_date) if snaps else (r.start, r.end)
    return Metrics(
        trading_days=len(snaps),
        initial=initial,
        final=final,
        total_return=final / initial - 1,
        cagr=cagr(initial, final, d0, d1),
        max_drawdown=max_drawdown([initial] + values),
        index_return=index_values[-1] / initial - 1,
        index_cagr=cagr(initial, index_values[-1], d0, d1),
        index_max_drawdown=max_drawdown(index_values),
        buys=sum(t.side == "BUY" for t in r.trades),
        sells=sum(t.side == "SELL" for t in r.trades),
        fees=round(sum(t.fees for t in r.trades), 2),
        wins=sum(t.pnl > 0 for t in trips),
        closed=len(trips),
        best=max(trips, key=lambda t: t.pnl, default=None),
        worst=min(trips, key=lambda t: t.pnl, default=None),
        exposure=(sum(s.positions_value > 0 for s in snaps) / len(snaps)) if snaps else 0.0,
        failed_days=sum(d.status == "failed" for d in r.days),
        skipped_days=sum(d.status == "skipped" for d in r.days),
    )


# ====================================================================== 输出


def _pct(v: Optional[float]) -> str:
    return "—" if v is None else f"{v:+.2%}"


def summary_text(r: BacktestResult, m: Metrics) -> str:
    st = r.settings
    win = f"{m.wins}/{m.closed}（{m.wins / m.closed:.0%}）" if m.closed else "—（还没有卖出）"
    lines = [
        f"=== {PROJECT_NAME} 回测 [{st.market}] {st.name} ｜模拟｜过去的成绩不代表未来 ===",
        f"期间：{r.start} ～ {r.end}（{m.trading_days} 个交易日）",
        f"策略：{st.strategy.short_window}MA / {st.strategy.long_window}MA 交叉，下一个交易日开盘价成交",
        f"本金：{money(st, m.initial)}  →  期末：{money(st, m.final)}",
        "",
        f"{'':14}{'Bot':>12}{'买入持有指数':>14}",
        f"{'总收益率':<12}{_pct(m.total_return):>12}{_pct(m.index_return):>14}",
        f"{'年化收益率':<11}{_pct(m.cagr):>12}{_pct(m.index_cagr):>14}",
        f"{'最大回撤':<12}{_pct(m.max_drawdown):>12}{_pct(m.index_max_drawdown):>14}",
        "",
        f"交易：买入 {m.buys} 次、卖出 {m.sells} 次；赚钱的卖出 {win}",
        f"费用合计：{money(st, m.fees)}；有持仓的日子：{m.exposure:.0%}",
    ]
    if m.best:
        lines.append(f"最好一笔：{m.best.ticker} {money(st, m.best.pnl, signed=True)}（{m.best.pnl_pct:+.1%}）")
        lines.append(f"最差一笔：{m.worst.ticker} {money(st, m.worst.pnl, signed=True)}（{m.worst.pnl_pct:+.1%}）")
    if r.open_positions:
        lines.append("期末持仓：" + "、".join(f"{t} {p.quantity} 股" for t, p in r.open_positions.items()))
    if m.failed_days:
        lines.append(f"⚠️ 有 {m.failed_days} 天因数据问题没有交易（和正式 bot 一样的保护）")
    for t, d0 in r.late_starters.items():
        lines.append(f"ℹ️ {t} 的数据从 {d0} 才开始（之前不在观察名单里）")
    return "\n".join(lines)


def _svg_chart(r: BacktestResult, width: int = 900, height: int = 300) -> str:
    """收益率曲线（%）：Bot vs 指数。纯 SVG，不需要网络。"""
    snaps = r.snapshots
    if len(snaps) < 2:
        return "<p class='muted'>数据太少，无法画图</p>"
    init, b0 = r.settings.initial_cash, snaps[0].benchmark_close
    bot = [(s.total_value / init - 1) * 100 for s in snaps]
    idx = [(s.benchmark_close / b0 - 1) * 100 for s in snaps]
    lo, hi = min(bot + idx + [0.0]), max(bot + idx + [0.0])
    pad = (hi - lo) * 0.08 or 1
    lo, hi = lo - pad, hi + pad
    L, R, T, B = 56, 12, 12, 28
    n = len(snaps)
    x = lambda i: L + (width - L - R) * i / (n - 1)
    y = lambda v: T + (height - T - B) * (hi - v) / (hi - lo)
    pts = lambda vals: " ".join(f"{x(i):.1f},{y(v):.1f}" for i, v in enumerate(vals))
    step = 10 ** math.floor(math.log10(max(hi - lo, 1e-9) / 4))
    step *= next(k for k in (1, 2, 5, 10) if (hi - lo) / (step * k) <= 8)
    grid = []
    v = math.ceil(lo / step) * step
    while v <= hi:
        grid.append(f"<line x1='{L}' x2='{width - R}' y1='{y(v):.1f}' y2='{y(v):.1f}' class='grid'/>"
                    f"<text x='{L - 6}' y='{y(v) + 4:.1f}' class='axis' text-anchor='end'>{v:g}%</text>")
        v += step
    years = sorted({s.snapshot_date.year for s in snaps})
    xt = []
    for yr in years:
        i = next(i for i, s in enumerate(snaps) if s.snapshot_date.year == yr)
        if i > 0 or len(years) == 1:
            xt.append(f"<text x='{x(i):.1f}' y='{height - 8}' class='axis' text-anchor='middle'>{yr}</text>")
    return (
        f"<svg viewBox='0 0 {width} {height}' role='img' aria-label='收益曲线 Bot vs 指数' class='chart'>"
        + "".join(grid) + "".join(xt)
        + f"<line x1='{L}' x2='{width - R}' y1='{y(0):.1f}' y2='{y(0):.1f}' class='zero'/>"
        + f"<polyline points='{pts(idx)}' class='line index'/>"
        + f"<polyline points='{pts(bot)}' class='line bot'/>"
        + "</svg>"
    )


def html_report(r: BacktestResult, m: Metrics) -> str:
    st = r.settings
    e = html.escape
    trips = {(t.ticker, t.sell_date): t for t in round_trips(r.trades)}
    rows = []
    for t in sorted(r.trades, key=lambda x: (x.trade_date, x.id or 0)):
        rt = trips.get((t.ticker, t.trade_date)) if t.side == "SELL" else None
        pnl = "" if rt is None else f"<span class='{'up' if rt.pnl > 0 else 'down'}'>{e(money(st, rt.pnl, signed=True))} ({rt.pnl_pct:+.1%})</span>"
        rows.append(
            f"<tr><td>{t.trade_date}</td><td>{e(t.ticker)}</td>"
            f"<td><span class='badge {t.side.lower()}'>{t.side}</span></td>"
            f"<td class='r'>{t.quantity:,}</td><td class='r'>{t.price:,.3f}</td>"
            f"<td class='r'>{e(money(st, t.quantity * t.price))}</td><td class='r'>{e(money(st, t.fees))}</td>"
            f"<td class='r'>{pnl}</td><td>{e(t.reason)}</td></tr>")
    trades_html = "".join(rows) or "<tr><td colspan='9' class='muted c'>这段期间没有任何交易</td></tr>"
    win = f"{m.wins}/{m.closed}（{m.wins / m.closed:.0%}）" if m.closed else "—"
    tone = lambda v: "" if v is None else ("up" if v > 0 else "down" if v < 0 else "")
    notes = []
    if m.failed_days:
        notes.append(f"有 {m.failed_days} 天因数据问题没有交易（和正式 bot 一样：宁可不交易）。")
    for t, d0 in r.late_starters.items():
        notes.append(f"{e(t)} 的数据从 {d0} 才开始（之前不在观察名单里）。")
    if r.open_positions:
        notes.append("期末仍持有：" + "、".join(f"{e(t)} {p.quantity:,} 股" for t, p in r.open_positions.items())
                     + "（已按最后收盘价计入总资产）。")
    notes.append("观察名单是今天挑的股票：用它回测过去会有“幸存者偏差”（当年表现差、后来下市的股票不在里面）。")
    notes.append("股价未复权（和正式 bot 一样），不含股息；指数也是价格指数，不含股息。")
    cfg = (f"本金 {e(money(st, st.initial_cash))}｜每手 {st.lot_size} 股｜单只上限 {st.risk.max_position_pct:.0%}｜"
           f"最多 {st.risk.max_positions} 只｜现金保留 {st.risk.min_cash_reserve_pct:.0%}｜止损 {st.risk.stop_loss_pct:.0%}")
    return f"""<!doctype html>
<html lang="zh"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>回测 {e(st.market)} · {PROJECT_NAME}</title>
<style>
:root{{--bg:#f6f7f9;--surface:#fff;--border:#e3e6eb;--text:#16181d;--muted:#646b78;--up:#15803d;--down:#c2410c;
--bot:#0f766e;--index:#94a3b8;--warn-bg:#fff7e6;--warn:#7a4b00;--warn-border:#f1c76b;color-scheme:light}}
@media (prefers-color-scheme:dark){{:root{{--bg:#0e1013;--surface:#161a1f;--border:#2a3038;--text:#e8eaed;--muted:#9aa3af;
--up:#4ade80;--down:#fb923c;--bot:#2dd4bf;--index:#64748b;--warn-bg:#2a2110;--warn:#f4cf7c;--warn-border:#6b5220;color-scheme:dark}}}}
*{{box-sizing:border-box}}body{{margin:0;background:var(--bg);color:var(--text);font:14px/1.5 system-ui,-apple-system,"Segoe UI",Roboto,"PingFang SC","Microsoft YaHei",sans-serif}}
.ribbon{{background:var(--warn-bg);color:var(--warn);border-bottom:1px solid var(--warn-border);text-align:center;font-weight:600;padding:8px 16px;font-size:13px}}
.wrap{{max-width:1100px;margin:0 auto;padding:20px 16px 48px}}h1{{font-size:22px;margin:0}}.muted{{color:var(--muted)}}
.card{{background:var(--surface);border:1px solid var(--border);border-radius:12px;padding:16px 18px;margin-top:16px}}
h2{{font-size:16px;margin:0 0 10px}}.grid3{{display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));gap:12px;margin-top:16px}}
.k{{font-size:12px;color:var(--muted);font-weight:600}}.v{{font-size:22px;font-weight:700;font-variant-numeric:tabular-nums}}
.up{{color:var(--up)}}.down{{color:var(--down)}}table{{width:100%;border-collapse:collapse;white-space:nowrap;font-variant-numeric:tabular-nums}}
th,td{{padding:7px 9px;border-bottom:1px solid var(--border);text-align:left}}th{{font-size:12px;color:var(--muted)}}.r{{text-align:right}}.c{{text-align:center}}
.scroll{{overflow-x:auto}}.badge{{padding:1px 8px;border-radius:999px;border:1px solid currentColor;font-size:12px;font-weight:600}}
.badge.buy{{color:var(--up)}}.badge.sell{{color:var(--down)}}
.chart{{width:100%;height:auto}}.grid{{stroke:var(--border)}}.zero{{stroke:var(--muted);stroke-dasharray:2 3}}
.axis{{fill:var(--muted);font-size:11px}}.line{{fill:none;stroke-width:2}}.bot{{stroke:var(--bot);stroke-width:2.5}}.index{{stroke:var(--index);stroke-dasharray:5 4}}
.legend span{{margin-right:16px}}.sw{{display:inline-block;width:18px;height:3px;vertical-align:middle;margin-right:6px}}
ul{{margin:0;padding-left:20px}}
</style></head><body>
<div class="ribbon">⚠️ 回测｜模拟｜虚拟资金 · Backtest with virtual money — 过去的成绩不代表未来，不构成投资建议</div>
<div class="wrap">
<h1>{e(st.flag)} {PROJECT_NAME} 回测 [{e(st.market)}] {e(st.name)}</h1>
<p class="muted">{r.start} ～ {r.end}（{m.trading_days} 个交易日）｜{st.strategy.short_window}MA / {st.strategy.long_window}MA 交叉，
今天收盘出信号、下一个交易日开盘价成交｜{cfg}</p>

<div class="grid3">
 <div class="card"><div class="k">期末总资产</div><div class="v">{e(money(st, m.final))}</div><div class="muted">本金 {e(money(st, m.initial))}</div></div>
 <div class="card"><div class="k">总收益率（Bot / 买入持有指数）</div><div class="v"><span class="{tone(m.total_return)}">{_pct(m.total_return)}</span> <span class="muted">/</span> <span class="{tone(m.index_return)}">{_pct(m.index_return)}</span></div><div class="muted">年化 {_pct(m.cagr)} / {_pct(m.index_cagr)}</div></div>
 <div class="card"><div class="k">最大回撤（Bot / 指数）</div><div class="v"><span class="down">{_pct(m.max_drawdown)}</span> <span class="muted">/</span> <span class="down">{_pct(m.index_max_drawdown)}</span></div><div class="muted">从最高点最多跌了多少</div></div>
 <div class="card"><div class="k">交易</div><div class="v">{m.buys} 买 · {m.sells} 卖</div><div class="muted">赚钱的卖出 {win}｜费用 {e(money(st, m.fees))}｜有持仓 {m.exposure:.0%} 的日子</div></div>
</div>

<div class="card"><h2>收益曲线（%）</h2>
<div class="legend muted"><span><i class="sw" style="background:var(--bot)"></i>Bot</span><span><i class="sw" style="background:var(--index)"></i>{e(st.benchmark)} 买入持有</span></div>
{_svg_chart(r)}</div>

<div class="card"><h2>说明</h2><ul>{''.join(f'<li>{n}</li>' for n in notes)}</ul></div>

<div class="card"><h2>每一笔交易（{len(r.trades)}）</h2><div class="scroll"><table>
<thead><tr><th>日期</th><th>代码</th><th>方向</th><th class="r">数量</th><th class="r">价格</th><th class="r">金额</th><th class="r">费用</th><th class="r">这次卖出盈亏</th><th>原因</th></tr></thead>
<tbody>{trades_html}</tbody></table></div></div>
<p class="muted c">{PROJECT_NAME} · 回测报告 · 产生时间 {datetime.now():%Y-%m-%d %H:%M}</p>
</div></body></html>
"""


# ====================================================================== CLI


def main(argv=None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except AttributeError:
            pass
    logging.basicConfig(level=logging.ERROR, format="%(levelname)s %(name)s: %(message)s")

    p = argparse.ArgumentParser(prog="python -m scripts.backtest", description=f"{PROJECT_NAME} 回测（只在本地跑）")
    p.add_argument("--market", required=True, help="市场代码（config/markets.yaml），例如 MY、US")
    p.add_argument("--years", type=float, default=DEFAULT_YEARS, help=f"回测过去几年（默认 {DEFAULT_YEARS}）")
    p.add_argument("--start", type=date.fromisoformat, help="开始日期 YYYY-MM-DD（优先于 --years）")
    p.add_argument("--end", type=date.fromisoformat, help="结束日期 YYYY-MM-DD（默认今天）")
    p.add_argument("--short", type=int, help="试别的短均线（不改 markets.yaml）")
    p.add_argument("--long", type=int, help="试别的长均线（不改 markets.yaml）")
    p.add_argument("--out", type=Path, default=REPORTS_DIR, help="HTML 报告放在哪个文件夹（默认 reports/）")
    p.add_argument("--open", action="store_true", help="跑完用浏览器打开报告")
    a = p.parse_args(argv)

    try:
        st = load_market(a.market)
    except ConfigError as e:
        print(f"配置错误：{e}")
        return 1
    if a.short or a.long:
        st = replace(st, strategy=replace(st.strategy, short_window=a.short or st.strategy.short_window,
                                          long_window=a.long or st.strategy.long_window))
        if not 0 < st.strategy.short_window < st.strategy.long_window:
            print("需要 0 < --short < --long")
            return 1
    end = a.end or datetime.now(ZoneInfo(st.timezone)).date()
    start = a.start or end - timedelta(days=round(a.years * 365.25))

    print(f"抓取 {st.market} {len(st.watchlist)} 只 + 指数 {st.benchmark}（{start} ～ {end}，含预热）…")
    try:
        data = fetch_range(st, start, end)
    except dfetch.DataError as e:
        print(f"⚠️ 数据抓取失败，无法回测：{e}")
        return 1

    def progress(i, n):
        if i == n or i % 250 == 0:
            print(f"  回放第 {i}/{n} 个交易日…")

    t0 = _time.time()
    result = run_backtest(st, data, start, end, progress)
    metrics = compute_metrics(result)
    print()
    print(summary_text(result, metrics))

    a.out.mkdir(parents=True, exist_ok=True)
    name = f"backtest_{st.market}_{result.start}_{result.end}"
    if a.short or a.long:
        name += f"_{st.strategy.short_window}-{st.strategy.long_window}"
    path = a.out / f"{name}.html"
    path.write_text(html_report(result, metrics), encoding="utf-8")
    print(f"\nHTML 报告：{path}（用时 {_time.time() - t0:.0f} 秒）")
    if a.open:
        import webbrowser

        webbrowser.open(path.resolve().as_uri())
    return 0


if __name__ == "__main__":
    sys.exit(main())
