"""策略。每个市场在 config/markets.yaml 的 strategy.name 选一个（见文件下方的注册表）。

均线交叉（ma_crossover），只在“交叉当天”产生信号：

- 50MA 由下往上穿过 200MA（金叉）当天 → BUY
- 50MA 由上往下穿过 200MA（死叉）当天 → SELL
- 数据不足 long_window + 1 天（需要前一天的均线来判断交叉）→ 不产生信号
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Literal, Optional

import pandas as pd

Side = Literal["BUY", "SELL"]


@dataclass(frozen=True)
class Signal:
    ticker: str
    side: Side
    reason: str
    signal_date: date
    close: float


def crossover(close: pd.Series, short_window: int, long_window: int) -> Optional[Side]:
    """判断最新一天是否发生均线交叉。"""
    if len(close) < long_window + 1:
        return None
    short_ma = close.rolling(short_window).mean()
    long_ma = close.rolling(long_window).mean()
    prev_s, prev_l = short_ma.iloc[-2], long_ma.iloc[-2]
    cur_s, cur_l = short_ma.iloc[-1], long_ma.iloc[-1]
    if any(pd.isna(x) for x in (prev_s, prev_l, cur_s, cur_l)):
        return None
    if prev_s <= prev_l and cur_s > cur_l:
        return "BUY"
    if prev_s >= prev_l and cur_s < cur_l:
        return "SELL"
    return None


def generate_signals(
    data: dict[str, pd.DataFrame],
    tickers: list[str] | tuple[str, ...],
    short_window: int,
    long_window: int,
) -> list[Signal]:
    """按 tickers（watchlist）顺序产生信号；不在 data 里的股票会被跳过。"""
    signals: list[Signal] = []
    for t in tickers:
        df = data.get(t)
        if df is None:
            continue
        side = crossover(df["Close"], short_window, long_window)
        if side is None:
            continue
        verb = "上穿" if side == "BUY" else "下穿"
        signals.append(
            Signal(
                ticker=t,
                side=side,
                reason=f"{short_window}MA {verb} {long_window}MA",
                signal_date=df.index[-1],
                close=float(df["Close"].iloc[-1]),
            )
        )
    return signals


# ====================================================================== 策略注册（每个市场在 markets.yaml 选一个）
#
# ma_crossover  均线交叉（马股用）：只在交叉当天买卖，持有到死叉或止损
# rsi_pullback  RSI 回调波段（短线）：上升趋势中急跌就买，反弹到短均线上方就卖，最多持有 N 个交易日
# rsi_daytrade  RSI 回调当天进出（短线）：前一天收盘出现急跌信号 → 今天开盘买、今天收盘卖


def rsi(close: pd.Series, period: int) -> pd.Series:
    """Wilder RSI（0～100）。数值越低代表最近跌得越急。"""
    delta = close.diff()
    gain = delta.clip(lower=0).ewm(alpha=1 / period, adjust=False, min_periods=period).mean()
    loss = (-delta.clip(upper=0)).ewm(alpha=1 / period, adjust=False, min_periods=period).mean()
    rs = gain / loss
    out = 100 - 100 / (1 + rs)
    return out.where(loss != 0, 100.0)


def history_needed(cfg) -> int:
    """这个策略至少需要几天数据才会产生信号。"""
    if cfg.name == "ma_crossover":
        return cfg.long_window + 1
    return max(cfg.trend_window, cfg.exit_window, cfg.rsi_period + 1) + 1


def same_day_exit(cfg) -> bool:
    """当天进出：开盘买进的仓位当天收盘就卖掉，不留过夜。"""
    return cfg.name == "rsi_daytrade"


def describe(cfg) -> str:
    """一句话说明策略（报告 / 讯息用）。"""
    if cfg.name == "ma_crossover":
        return f"{cfg.short_window}MA / {cfg.long_window}MA 交叉，下一个交易日开盘价成交"
    entry = (f"RSI({cfg.rsi_period}) < {cfg.rsi_entry:g} 而且股价在 {cfg.trend_window} 日均线上方"
             f"（上升趋势中的急跌）")
    if cfg.name == "rsi_daytrade":
        return f"当天进出：前一天收盘 {entry} → 今天开盘买、今天收盘卖"
    return (f"短线波段：收盘 {entry} → 下一个交易日开盘买；收盘高于 {cfg.exit_window} 日均线"
            f"或持有满 {cfg.max_hold_days} 个交易日 → 下一个交易日开盘卖")


def _rsi_signals(cfg, data, tickers, positions, today) -> list[Signal]:
    out: list[Signal] = []
    for t in tickers:
        df = data.get(t)
        if df is None or len(df) < history_needed(cfg):
            continue
        close = df["Close"]
        c = float(close.iloc[-1])
        when = df.index[-1]
        pos = positions.get(t)
        if pos is not None and not same_day_exit(cfg):
            exit_ma = float(close.rolling(cfg.exit_window).mean().iloc[-1])
            held = int((pd.Index(df.index) > pos.buy_date).sum())      # 买入后过了几个交易日
            if c > exit_ma:
                out.append(Signal(t, "SELL", f"收盘高于 {cfg.exit_window} 日均线（已反弹）", when, c))
            elif held >= cfg.max_hold_days:
                out.append(Signal(t, "SELL", f"持有满 {cfg.max_hold_days} 个交易日", when, c))
            continue
        if pos is not None:
            continue
        trend = close.rolling(cfg.trend_window).mean().iloc[-1]
        r = rsi(close, cfg.rsi_period).iloc[-1]
        if pd.isna(trend) or pd.isna(r):
            continue
        if c > float(trend) and float(r) < cfg.rsi_entry:
            tag = "当天进出：" if same_day_exit(cfg) else ""
            out.append(Signal(t, "BUY", f"{tag}RSI({cfg.rsi_period}) {float(r):.1f} < {cfg.rsi_entry:g}，"
                                        f"股价在 {cfg.trend_window} 日均线上方（回调）", when, c))
    return out


def strategy_signals(cfg, data: dict[str, pd.DataFrame], tickers, positions: dict, today: date) -> list[Signal]:
    """按市场设定的策略产生信号（按 watchlist 顺序）。"""
    if cfg.name == "ma_crossover":
        return generate_signals(data, tickers, cfg.short_window, cfg.long_window)
    if cfg.name in ("rsi_pullback", "rsi_daytrade"):
        return _rsi_signals(cfg, data, tickers, positions, today)
    raise ValueError(f"不支持的策略：{cfg.name}")
