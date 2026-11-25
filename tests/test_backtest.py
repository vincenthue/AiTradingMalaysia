"""回测：用假数据（不连网络）检查回放、没有偷看答案、指标、报告。"""
from datetime import date

import pandas as pd
import pytest

from bot.models import Trade
from scripts.backtest import (
    compute_metrics,
    html_report,
    max_drawdown,
    round_trips,
    run_backtest,
    summary_text,
)
from tests.conftest import golden_cross_closes, make_df


def _data(settings, n=40, end=date(2026, 11, 24)):
    """指数平稳；1155.KL 在倒数第 6 天出现金叉，之后平稳；其他股票平稳。"""
    data = {t: make_df([10.0] * n, end) for t in settings.watchlist}
    gc = golden_cross_closes(n_flat=n - 6)              # 长度 n-5，最后一天是金叉
    closes = gc + [gc[-1]] * 5
    opens = closes[:]                                    # 默认开盘 = 收盘
    df = make_df(closes, end, opens=opens)
    cross_day = df.index[n - 6]
    next_day = df.index[n - 5]
    df.loc[next_day, "Open"] = 12.34                     # 金叉隔天的开盘价（要用这个成交）
    data["1155.KL"] = df
    data[settings.benchmark] = make_df([1500.0 + i for i in range(n)], end)
    return data, cross_day, next_day


def test_backtest_fills_next_day_at_open(small_settings):
    st = small_settings
    data, cross_day, next_day = _data(st)
    r = run_backtest(st, data, start=data[st.benchmark].index[12], end=data[st.benchmark].index[-1])
    buys = [t for t in r.trades if t.side == "BUY"]
    assert len(buys) == 1 and buys[0].ticker == "1155.KL"
    assert buys[0].trade_date == next_day and buys[0].price == 12.34   # 不是金叉当天收盘价
    assert all(t.trade_date > cross_day for t in r.trades)
    assert len(r.snapshots) == len(r.days) and all(d.status == "success" for d in r.days)


def test_backtest_handles_ticker_listed_later(small_settings):
    st = small_settings
    data, *_ = _data(st)
    late = data["6888.KL"].iloc[25:]                     # 这只股票晚 25 天才有数据
    data["6888.KL"] = late
    r = run_backtest(st, data, start=data[st.benchmark].index[12], end=data[st.benchmark].index[-1])
    assert r.late_starters == {"6888.KL": late.index[0]}
    assert all(d.status == "success" for d in r.days)


def test_backtest_too_short_period_rejected(small_settings):
    data, *_ = _data(small_settings)
    last = data[small_settings.benchmark].index[-1]
    with pytest.raises(ValueError):
        run_backtest(small_settings, data, start=last, end=last)


def test_backtest_uses_only_in_memory_db(small_settings, monkeypatch):
    """回测不能碰 Supabase / Telegram。"""
    import bot.db as dbmod
    import bot.notifier as nmod

    def boom(*a, **k):
        raise AssertionError("回测不应该建立 Supabase / Telegram 连线")

    monkeypatch.setattr(dbmod, "SupabaseDB", boom)
    monkeypatch.setattr(nmod, "TelegramNotifier", boom)
    data, *_ = _data(small_settings)
    run_backtest(small_settings, data, start=data[small_settings.benchmark].index[12],
                 end=data[small_settings.benchmark].index[-1])


def test_metrics_and_reports(small_settings):
    st = small_settings
    data, *_ = _data(st)
    r = run_backtest(st, data, start=data[st.benchmark].index[12], end=data[st.benchmark].index[-1])
    m = compute_metrics(r)
    assert m.buys == 1 and m.sells == 0 and m.fees > 0
    assert m.trading_days == len(r.snapshots)
    assert m.index_return > 0                          # 假指数一直涨
    text = summary_text(r, m)
    assert "过去的成绩不代表未来" in text and "1155.KL" in text
    page = html_report(r, m)
    assert page.startswith("<!doctype html>") and "回测｜模拟｜虚拟资金" in page
    assert "<polyline" in page and "1155.KL" in page and "<script" not in page


def test_max_drawdown():
    assert max_drawdown([100, 120, 90, 130, 117]) == pytest.approx(90 / 120 - 1)
    assert max_drawdown([100, 101, 102]) == 0


def test_round_trips_include_fees_on_both_sides():
    d = date(2026, 1, 1)
    trades = [
        Trade(d, "X", "BUY", 100, 1.00, 8.0, "", 1),
        Trade(d.replace(day=2), "X", "BUY", 100, 2.00, 8.0, "", 2),
        Trade(d.replace(day=3), "X", "SELL", 200, 2.00, 9.0, "止损", 3),
    ]
    [rt] = round_trips(trades)
    assert rt.pnl == round(400 - 9 - (100 + 8 + 200 + 8), 2)    # = 75
    assert rt.buy_date == d and rt.reason == "止损"
