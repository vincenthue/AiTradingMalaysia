from datetime import date

import pandas as pd

from bot.strategy import crossover, generate_signals
from tests.conftest import death_cross_closes, golden_cross_closes, make_df

TODAY = date(2026, 11, 24)


def test_golden_cross_buy():
    assert crossover(pd.Series(golden_cross_closes()), 5, 10) == "BUY"


def test_death_cross_sell():
    assert crossover(pd.Series(death_cross_closes()), 5, 10) == "SELL"


def test_no_signal_when_already_above():
    """5MA 早就在 10MA 之上（不是今天才穿过）→ 不产生信号。"""
    closes = [10 + 0.1 * i for i in range(30)]
    assert crossover(pd.Series(closes), 5, 10) is None


def test_no_signal_flat():
    assert crossover(pd.Series([10.0] * 30), 5, 10) is None


def test_insufficient_data_no_signal():
    closes = golden_cross_closes()[-10:]  # 只有 10 天，需要 11 天
    assert crossover(pd.Series(closes), 5, 10) is None


def test_generate_signals_watchlist_order_and_reason():
    data = {
        "B.KL": make_df(golden_cross_closes(), TODAY),
        "A.KL": make_df(death_cross_closes(), TODAY),
        "C.KL": make_df([10.0] * 20, TODAY),
    }
    sigs = generate_signals(data, ["A.KL", "B.KL", "C.KL", "MISSING.KL"], 5, 10)
    assert [(s.ticker, s.side) for s in sigs] == [("A.KL", "SELL"), ("B.KL", "BUY")]
    assert sigs[1].reason == "5MA 上穿 10MA"
    assert sigs[0].reason == "5MA 下穿 10MA"
    assert sigs[1].signal_date == TODAY
