"""抓股价（yfinance）+ 数据验证。

规则（PROJECT_SPEC 第 7.1 节）：
- 每个 ticker 失败后最多重试 3 次（指数退避 5、15、45 秒），还是失败 → 整次运行停止。
- 价格不能是 NaN、0 或负数；成交量不能是负数 → 整次运行停止。
- 单日涨跌超过 30% → 跳过该股并警报（其他股照常）。
- 数据不足 long_window 天 → 该股不产生信号。
- 最新数据日期超过 5 天 → 视为过期，警报并停止。
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import date
from typing import Callable, Iterable, Sequence

import pandas as pd

log = logging.getLogger(__name__)

PRICE_COLUMNS = ("Open", "High", "Low", "Close")
REQUIRED_COLUMNS = PRICE_COLUMNS + ("Volume",)

YFINANCE_HINT = "可能需要更新 yfinance 版本（修改 requirements.txt 里的 yfinance==x.y.z）"


class DataError(Exception):
    """所有数据问题的基类。出现时一律不交易。"""


class DataFetchError(DataError):
    def __init__(self, ticker: str, attempts: int, last_error: BaseException | str):
        self.ticker = ticker
        self.attempts = attempts
        self.last_error = last_error
        err = (
            f"{type(last_error).__name__}: {last_error}"
            if isinstance(last_error, BaseException)
            else str(last_error)
        )
        super().__init__(
            f"{ticker} 抓取失败（共尝试 {attempts} 次）。最后错误：{err}。"
            f"请检查 ticker 是否正确；{YFINANCE_HINT}"
        )


class DataValidationError(DataError):
    pass


class StaleDataError(DataError):
    pass


Downloader = Callable[[str, str], pd.DataFrame]


def yfinance_downloader(ticker: str, period: str) -> pd.DataFrame:
    """真实的 yfinance 抓取（测试时不会用到）。使用未复权价格。"""
    import yfinance as yf

    return yf.Ticker(ticker).history(period=period, interval="1d", auto_adjust=False)


def normalize(df: pd.DataFrame, ticker: str) -> pd.DataFrame:
    """统一格式：index 是 date（交易所当地日期），只保留 OHLCV，按日期排序。"""
    if df is None or df.empty:
        raise DataValidationError(f"{ticker} 没有任何数据（ticker 可能不存在或已下市）")
    missing = [c for c in REQUIRED_COLUMNS if c not in df.columns]
    if missing:
        raise DataValidationError(f"{ticker} 数据缺少栏位：{missing}。{YFINANCE_HINT}")
    out = df.loc[:, list(REQUIRED_COLUMNS)].copy()
    idx = pd.DatetimeIndex(out.index)
    out.index = pd.Index([ts.date() for ts in idx], name="Date")
    out = out[~out.index.duplicated(keep="last")].sort_index()
    # 整行价格都是 NaN 的通常是休市占位行，直接移除；部分 NaN 会在验证时报错
    out = out.dropna(how="all", subset=list(PRICE_COLUMNS))
    if out.empty:
        raise DataValidationError(f"{ticker} 数据全部是空值")
    return out


def fetch_history(
    ticker: str,
    period: str,
    downloader: Downloader = yfinance_downloader,
    retry_delays: Sequence[float] = (5, 15, 45),
    sleep: Callable[[float], None] = time.sleep,
) -> pd.DataFrame:
    """抓一个 ticker；失败（异常或空数据）就按 retry_delays 重试。"""
    attempts = 1 + len(retry_delays)
    last_error: BaseException | str = "unknown"
    for attempt in range(1, attempts + 1):
        try:
            raw = downloader(ticker, period)
            return normalize(raw, ticker)
        except Exception as exc:  # noqa: BLE001 - yfinance 可能抛任何异常
            last_error = exc
            log.warning("抓取 %s 失败（第 %d/%d 次）：%s", ticker, attempt, attempts, exc)
            if attempt < attempts:
                sleep(retry_delays[attempt - 1])
    raise DataFetchError(ticker, attempts, last_error)


def fetch_all(
    tickers: Iterable[str],
    period: str,
    downloader: Downloader = yfinance_downloader,
    retry_delays: Sequence[float] = (5, 15, 45),
    sleep: Callable[[float], None] = time.sleep,
) -> dict[str, pd.DataFrame]:
    """抓全部 ticker。任何一个最终失败 → 抛 DataFetchError（整次运行停止）。"""
    return {
        t: fetch_history(t, period, downloader, retry_delays, sleep) for t in tickers
    }


# ----------------------------------------------------------------- 验证


def validate_frame(ticker: str, df: pd.DataFrame) -> None:
    """硬性检查：价格 NaN / 0 / 负数、成交量负数 → DataValidationError。"""
    prices = df.loc[:, list(PRICE_COLUMNS)]
    if prices.isna().any().any():
        bad = prices[prices.isna().any(axis=1)].index[-1]
        raise DataValidationError(f"{ticker} 在 {bad} 的价格有 NaN")
    if (prices <= 0).any().any():
        bad = prices[(prices <= 0).any(axis=1)].index[-1]
        raise DataValidationError(f"{ticker} 在 {bad} 的价格是 0 或负数")
    vol = df["Volume"]
    if (vol < 0).any():
        bad = vol[vol < 0].index[-1]
        raise DataValidationError(f"{ticker} 在 {bad} 的成交量是负数")


def latest_daily_move(df: pd.DataFrame) -> float | None:
    """最新一天收盘价相对前一天的涨跌幅；数据不足两天返回 None。"""
    if len(df) < 2:
        return None
    prev, last = float(df["Close"].iloc[-2]), float(df["Close"].iloc[-1])
    return last / prev - 1.0


def check_staleness(ticker: str, latest: date, today: date, max_stale_days: int) -> None:
    age = (today - latest).days
    if age > max_stale_days:
        raise StaleDataError(
            f"{ticker} 最新数据是 {latest}，已经 {age} 天没有更新（上限 {max_stale_days} 天）。"
            f"yfinance 可能坏了；{YFINANCE_HINT}"
        )


@dataclass
class ValidationReport:
    """验证结果。

    usable:      通过验证、可以用来交易的股票
    suspicious:  单日涨跌异常，今天跳过的股票 → 原因
    insufficient: 数据天数不足，不产生策略信号的股票（止损仍会检查）
    warnings:    需要发警报但不停止运行的讯息
    """

    usable: dict[str, pd.DataFrame] = field(default_factory=dict)
    suspicious: dict[str, str] = field(default_factory=dict)
    insufficient: set[str] = field(default_factory=set)
    warnings: list[str] = field(default_factory=list)


def validate_all(
    data: dict[str, pd.DataFrame],
    benchmark: str,
    today: date,
    long_window: int,
    max_daily_move_pct: float,
    max_stale_days: int,
) -> ValidationReport:
    """对全部数据（含指数）做验证。硬性错误直接抛异常。"""
    report = ValidationReport()
    for ticker, df in data.items():
        validate_frame(ticker, df)
        check_staleness(ticker, df.index[-1], today, max_stale_days)

        move = latest_daily_move(df)
        if move is not None and abs(move) > max_daily_move_pct:
            reason = (
                f"{ticker} 在 {df.index[-1]} 单日涨跌 {move:+.1%}，"
                f"超过 {max_daily_move_pct:.0%}，视为可疑"
            )
            if ticker == benchmark:
                report.warnings.append(reason + "（指数只用于比较，不影响交易）")
                report.usable[ticker] = df
            else:
                report.suspicious[ticker] = reason
            continue

        report.usable[ticker] = df
        if ticker != benchmark and len(df) < long_window + 1:
            report.insufficient.add(ticker)
    return report
