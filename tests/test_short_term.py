"""短线策略：RSI 回调波段（rsi_pullback）、RSI 当天进出（rsi_daytrade）。全部假数据。"""
from dataclasses import replace
from datetime import date

import pandas as pd
import pytest
import yaml

from bot.config import DEFAULT_MARKETS_PATH, ConfigError, parse_market
from bot.db import InMemoryDB
from bot.fees import calculate_fees
from bot.main import execute
from bot.models import Position
from bot.strategy import history_needed, rsi, same_day_exit, strategy_signals
from tests.conftest import make_df
from tests.test_main import Quiet, at, market, no_sleep

MON, TUE, WED = date(2026, 11, 23), date(2026, 11, 24), date(2026, 11, 25)


def _cfg(st, name, **kw):
    """短参数版本，方便造数据：趋势 20 日、出场 5 日、RSI(2) < 10。"""
    return replace(st, strategy=replace(st.strategy, name=name, trend_window=20, exit_window=5,
                                        rsi_period=2, rsi_entry=10.0, max_hold_days=3, **kw))


def uptrend_then_dip(n=40, end=TUE):
    """长期上升，最后两天急跌（RSI(2) 很低，但还在 20 日均线上方）。"""
    closes = [10.0 + 0.1 * i for i in range(n - 2)]
    closes += [closes[-1] - 0.4, closes[-1] - 0.8]
    return make_df(closes, end)


# ------------------------------------------------------------ RSI 本身


def test_rsi_bounds_and_direction():
    up = pd.Series([float(x) for x in range(1, 30)])
    down = pd.Series([float(x) for x in range(30, 1, -1)])
    assert rsi(up, 2).iloc[-1] == 100
    assert rsi(down, 2).iloc[-1] < 1
    mixed = rsi(pd.Series([10, 11, 10.5, 11.5, 11, 12, 11.2, 12.5] * 3, dtype=float), 2).dropna()
    assert ((mixed >= 0) & (mixed <= 100)).all()


# ------------------------------------------------------------ 信号


def test_pullback_buys_dip_in_uptrend(small_settings):
    st = _cfg(small_settings, "rsi_pullback")
    df = uptrend_then_dip()
    [sig] = strategy_signals(st.strategy, {"X": df}, ["X"], {}, TUE)
    assert sig.side == "BUY" and "RSI(2)" in sig.reason and sig.close == df["Close"].iloc[-1]


def test_pullback_ignores_dip_in_downtrend(small_settings):
    st = _cfg(small_settings, "rsi_pullback")
    closes = [20.0 - 0.1 * i for i in range(40)]            # 一直跌：在趋势线下面
    assert strategy_signals(st.strategy, {"X": make_df(closes, TUE)}, ["X"], {}, TUE) == []


def test_pullback_exits_on_bounce_or_max_hold(small_settings):
    st = _cfg(small_settings, "rsi_pullback")
    bounce = make_df([10.0] * 30 + [10.5], TUE)             # 收盘高于 5 日均线
    pos = {"X": Position("X", 5, 10.0, MON)}
    [sig] = strategy_signals(st.strategy, {"X": bounce}, ["X"], pos, TUE)
    assert sig.side == "SELL" and "5 日均线" in sig.reason
    flat_low = make_df([10.0] * 30 + [9.9, 9.8, 9.7, 9.6], TUE)   # 没反弹，但已经持有 4 天 ≥ 3
    old = {"X": Position("X", 5, 10.0, flat_low.index[-5])}
    [sig] = strategy_signals(st.strategy, {"X": flat_low}, ["X"], old, TUE)
    assert sig.side == "SELL" and "持有满 3" in sig.reason


def test_not_enough_history_no_signal(small_settings):
    st = _cfg(small_settings, "rsi_pullback")
    short = uptrend_then_dip(n=history_needed(st.strategy) - 1)
    assert strategy_signals(st.strategy, {"X": short}, ["X"], {}, TUE) == []


# ------------------------------------------------------------ 完整流程


def test_pullback_full_cycle_next_day_open(small_settings):
    st = _cfg(small_settings, "rsi_pullback")
    db, n = InMemoryDB(st.initial_cash, "MY"), Quiet()
    dip = uptrend_then_dip()
    execute(st, db, n, now=at(TUE), downloader=market(st, TUE, {"1155.KL": dip}), sleep=no_sleep)
    [o] = db.get_pending_orders()
    assert o.side == "BUY" and o.ticker == "1155.KL"
    nxt = make_df(list(dip["Close"]) + [float(dip["Close"].iloc[-1]) + 1.5], WED)
    nxt.loc[WED, "Open"] = 12.0
    r = execute(st, db, n, now=at(WED), downloader=market(st, WED, {"1155.KL": nxt}), sleep=no_sleep)
    assert [t.side for t in r.trades] == ["BUY"] and r.trades[0].price == 12.0
    [s] = db.get_pending_orders()                               # 同一天收盘反弹 → 明天卖
    assert s.side == "SELL"


def test_daytrade_buys_at_open_and_sells_at_close_same_day(small_settings):
    st = _cfg(small_settings, "rsi_daytrade")
    assert same_day_exit(st.strategy)
    db, n = InMemoryDB(st.initial_cash, "MY"), Quiet()
    dip = uptrend_then_dip()
    execute(st, db, n, now=at(TUE), downloader=market(st, TUE, {"1155.KL": dip}), sleep=no_sleep)
    [o] = db.get_pending_orders()
    assert o.side == "BUY" and o.reason.startswith("当天进出")
    nxt = make_df(list(dip["Close"]) + [13.0], WED)
    nxt.loc[WED, ["Open", "Low", "High"]] = [12.0, 11.9, 13.1]
    r = execute(st, db, n, now=at(WED), downloader=market(st, WED, {"1155.KL": nxt}), sleep=no_sleep)
    buy, sell = r.trades
    assert (buy.side, buy.price, buy.trade_date) == ("BUY", 12.0, WED)
    assert (sell.side, sell.price, sell.trade_date) == ("SELL", 13.0, WED)
    assert sell.reason == "当天收盘平仓" and db.get_positions() == {}
    fees = calculate_fees(buy.quantity * 12.0, st.fees, "BUY").total + calculate_fees(sell.quantity * 13.0, st.fees, "SELL").total
    assert db.get_account().cash == round(st.initial_cash + buy.quantity * 1.0 - fees, 2)


def test_daytrade_intraday_stop_loss(small_settings):
    st = _cfg(small_settings, "rsi_daytrade")
    db, n = InMemoryDB(st.initial_cash, "MY"), Quiet()
    dip = uptrend_then_dip()
    execute(st, db, n, now=at(TUE), downloader=market(st, TUE, {"1155.KL": dip}), sleep=no_sleep)
    nxt = make_df(list(dip["Close"]) + [11.5], WED)
    nxt.loc[WED, ["Open", "Low", "High"]] = [12.0, 10.5, 12.1]      # 盘中跌超过 8%
    r = execute(st, db, n, now=at(WED), downloader=market(st, WED, {"1155.KL": nxt}), sleep=no_sleep)
    sell = r.trades[-1]
    assert sell.side == "SELL" and sell.price == round(12.0 * 0.92, 4) and "当天止损" in sell.reason


# ------------------------------------------------------------ 配置


def _raw():
    return yaml.safe_load(DEFAULT_MARKETS_PATH.read_text(encoding="utf-8"))


def test_config_selects_strategy_per_market():
    raw = _raw()
    raw["markets"]["US"]["strategy"] = {"name": "rsi_pullback", "rsi_entry": 5, "max_hold_days": 7}
    us = parse_market(raw, "US")
    my = parse_market(raw, "MY")
    assert us.strategy.name == "rsi_pullback" and us.strategy.rsi_entry == 5.0 and us.strategy.max_hold_days == 7
    assert us.strategy.trend_window == 200                      # 没写的用默认
    assert my.strategy.name == "ma_crossover"                   # 马股不受影响


@pytest.mark.parametrize("bad", [{"name": "magic"}, {"name": "rsi_pullback", "rsi_entry": 150},
                                 {"name": "rsi_daytrade", "typo_key": 1}])
def test_bad_strategy_config_rejected(bad):
    raw = _raw()
    raw["markets"]["US"]["strategy"] = bad
    with pytest.raises(ConfigError):
        parse_market(raw, "US")
