"""策略：均线交叉（ma_crossover），只在“交叉当天”产生信号。

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
