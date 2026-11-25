from datetime import date

import numpy as np
import pandas as pd
import pytest

from bot import data_fetcher as dfetch
from tests.conftest import make_df

TODAY = date(2026, 11, 24)


# ------------------------------------------------------------ 抓取 + 重试


def test_retry_then_success():
    calls, sleeps = [], []

    def downloader(ticker, period):
        calls.append(ticker)
        if len(calls) < 3:
            raise ConnectionError("network down")
        return make_df([1, 2, 3], TODAY)

    df = dfetch.fetch_history("1155.KL", "2y", downloader, (5, 15, 45), sleeps.append)
    assert len(df) == 3
    assert sleeps == [5, 15]  # 指数退避


def test_retry_exhausted_raises_with_hint():
    sleeps = []

    def downloader(ticker, period):
        raise RuntimeError("yfinance exploded")

    with pytest.raises(dfetch.DataFetchError) as ei:
        dfetch.fetch_history("1155.KL", "2y", downloader, (5, 15, 45), sleeps.append)
    assert sleeps == [5, 15, 45]
    assert ei.value.attempts == 4
    msg = str(ei.value)
    assert "yfinance exploded" in msg and "yfinance 版本" in msg


def test_empty_dataframe_is_failure():
    """错误的 ticker：yfinance 不报错、只回传空表 → 也要当成失败。"""
    with pytest.raises(dfetch.DataFetchError, match="9999X.KL"):
        dfetch.fetch_history("9999X.KL", "2y", lambda t, p: pd.DataFrame(), (0, 0, 0), lambda s: None)


def test_fetch_all_stops_on_any_failure():
    def downloader(ticker, period):
        if ticker == "BAD.KL":
            return pd.DataFrame()
        return make_df([1, 2], TODAY)

    with pytest.raises(dfetch.DataFetchError):
        dfetch.fetch_all(["1155.KL", "BAD.KL"], "2y", downloader, (0,), lambda s: None)


def test_normalize_handles_tz_index_and_all_nan_rows():
    idx = pd.DatetimeIndex(["2026-11-21", "2026-11-23", "2026-11-24"]).tz_localize("Asia/Kuala_Lumpur")
    raw = pd.DataFrame(
        {"Open": [1.0, np.nan, 1.2], "High": [1.0, np.nan, 1.2], "Low": [1.0, np.nan, 1.2],
         "Close": [1.0, np.nan, 1.2], "Volume": [1, 0, 1], "Dividends": [0, 0, 0]},
        index=idx,
    )
    df = dfetch.normalize(raw, "X")
    assert list(df.index) == [date(2026, 11, 21), date(2026, 11, 24)]
    assert list(df.columns) == ["Open", "High", "Low", "Close", "Volume"]


def test_missing_columns_rejected():
    raw = make_df([1, 2], TODAY).drop(columns=["Volume"])
    with pytest.raises(dfetch.DataValidationError, match="yfinance"):
        dfetch.normalize(raw, "X")


# ------------------------------------------------------------ 数据验证


@pytest.mark.parametrize("bad", [np.nan, 0.0, -1.0])
def test_bad_price_rejected(bad):
    df = make_df([1.0, 1.1, 1.2], TODAY)
    df.loc[df.index[1], "Close"] = bad
    with pytest.raises(dfetch.DataValidationError):
        dfetch.validate_frame("X", df)


def test_negative_volume_rejected():
    df = make_df([1.0, 1.1], TODAY, volumes=[100, -5])
    with pytest.raises(dfetch.DataValidationError, match="成交量"):
        dfetch.validate_frame("X", df)


def test_zero_volume_allowed():
    dfetch.validate_frame("X", make_df([1.0, 1.1], TODAY, volumes=[0, 0]))


def test_stale_data():
    with pytest.raises(dfetch.StaleDataError, match="yfinance"):
        dfetch.check_staleness("X", date(2026, 11, 18), TODAY, 5)
    dfetch.check_staleness("X", date(2026, 11, 19), TODAY, 5)  # 刚好 5 天 OK


def _validate(data, long_window=10):
    return dfetch.validate_all(data, "^KLSE", TODAY, long_window, 0.30, 5)


def test_big_move_skips_only_that_stock():
    data = {
        "A.KL": make_df([10] * 20 + [14], TODAY),     # +40% → 可疑
        "B.KL": make_df([10] * 20 + [10.5], TODAY),
        "^KLSE": make_df([1500] * 21, TODAY),
    }
    rep = _validate(data)
    assert "A.KL" in rep.suspicious and "A.KL" not in rep.usable
    assert "B.KL" in rep.usable


def test_big_move_on_benchmark_only_warns():
    data = {"^KLSE": make_df([1500] * 20 + [800], TODAY)}
    rep = _validate(data)
    assert "^KLSE" in rep.usable and rep.warnings


def test_insufficient_history_flagged():
    data = {"A.KL": make_df([10] * 5, TODAY), "^KLSE": make_df([1500] * 5, TODAY)}
    rep = _validate(data, long_window=10)
    assert rep.insufficient == {"A.KL"}
