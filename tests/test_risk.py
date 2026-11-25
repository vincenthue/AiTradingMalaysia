from dataclasses import replace
from datetime import date

import pytest

from bot.broker import calculate_fees
from bot.models import Position
from bot.risk import (
    RiskViolation,
    buy_cost,
    cash_reserve,
    max_buy_quantity,
    stop_loss_triggered,
    validate_buy,
    validate_sell,
)

D = date(2026, 11, 1)


# ------------------------------------------------------------ 费用


def test_fees_minimum_brokerage(settings):
    f = calculate_fees(1000, settings.fees, "BUY")       # 0.1% = RM1 < RM8 → 收 RM8
    assert f.brokerage == 8.0
    assert f.clearing == 0.30
    assert f.stamp_duty == 1.00
    assert f.total == 9.30


def test_fees_stamp_duty_proportional(settings):
    f = calculate_fees(2350, settings.fees, "BUY")
    assert f.stamp_duty == 2.35


def test_fees_large_trade(settings):
    f = calculate_fees(20000, settings.fees, "BUY")
    assert f.brokerage == 20.0 and f.clearing == 6.0 and f.stamp_duty == 20.0


def test_fees_stamp_duty_cap(settings):
    f = calculate_fees(5_000_000, settings.fees, "BUY")
    assert f.stamp_duty == 1000


def test_my_fees_same_on_sell_no_sec_fee(settings):
    """马股卖出和买入收一样的费用，没有 SEC fee。"""
    assert calculate_fees(1000, settings.fees, "SELL") == calculate_fees(1000, settings.fees, "BUY")
    assert calculate_fees(1000, settings.fees, "SELL").sec_fee == 0


def test_fees_reject_bad_side(settings):
    with pytest.raises(ValueError):
        calculate_fees(1000, settings.fees, "SHORT")


# ------------------------------------------------------------ 买入风控


def _buy(settings, qty, price, cash=10000, total=10000, positions=None, **kw):
    validate_buy("X.KL", qty, price, cash, total, positions or {}, settings.risk,
                 settings.fees, settings.lot_size, **kw)


def test_buy_ok(settings):
    _buy(settings, 100, 10.0)  # RM1,000 + 费用，远低于 20%


def test_buy_not_enough_cash(settings):
    with pytest.raises(RiskViolation, match="现金不足"):
        _buy(settings, 100, 10.0, cash=500, total=100000)


def test_buy_below_cash_reserve(settings):
    with pytest.raises(RiskViolation, match="保留额"):
        _buy(settings, 100, 10.0, cash=1200, total=100000)


def test_buy_odd_lot(settings):
    with pytest.raises(RiskViolation, match="整手"):
        _buy(settings, 150, 1.0)


def test_buy_over_position_pct(settings):
    with pytest.raises(RiskViolation, match="20%"):
        _buy(settings, 300, 10.0)  # RM3,000 > RM2,000


def test_buy_over_max_positions(settings):
    positions = {f"P{i}.KL": Position(f"P{i}.KL", 100, 1.0, D) for i in range(5)}
    with pytest.raises(RiskViolation, match="持仓数"):
        _buy(settings, 100, 1.0, positions=positions)


def test_buy_max_positions_counts_pending(settings):
    positions = {f"P{i}.KL": Position(f"P{i}.KL", 100, 1.0, D) for i in range(3)}
    with pytest.raises(RiskViolation, match="持仓数"):
        _buy(settings, 100, 1.0, positions=positions, extra_new_positions=2)


def test_max_buy_quantity_targets_20pct(settings):
    qty = max_buy_quantity("X.KL", 13.20, 10000, 10000, {}, settings.risk, settings.fees, 100)
    assert qty == 100          # 200 股 = RM2,640 > RM2,000
    qty = max_buy_quantity("X.KL", 1.61, 10000, 10000, {}, settings.risk, settings.fees, 100)
    assert qty == 1200         # RM1,932 ≤ RM2,000
    assert qty % 100 == 0


def test_max_buy_quantity_limited_by_cash(settings):
    qty = max_buy_quantity("X.KL", 1.0, 1000, 10000, {}, settings.risk, settings.fees, 100)
    # 保留额 = 总资产 10000 × 5% = 500 → 可用 500，还要扣费用 → 400 股（400 + 8.52 费用）
    assert qty == 400
    assert 1000 - buy_cost(qty, 1.0, settings.fees) >= cash_reserve(10000, settings.risk)


def test_cash_reserve_is_pct_of_total_assets(settings):
    assert cash_reserve(10000, settings.risk) == 500
    assert cash_reserve(200, settings.risk) == 10
    # 总资产变大 → 保留额跟着变大：同样现金 1000，总资产 20000 时保留 1000 → 买不了
    assert max_buy_quantity("X.KL", 1.0, 1000, 20000, {}, settings.risk, settings.fees, 100) == 0


def test_max_buy_quantity_zero_when_unaffordable(settings):
    assert max_buy_quantity("X.KL", 50.0, 10000, 10000, {}, settings.risk, settings.fees, 100) == 0


# ------------------------------------------------------------ 卖出风控


def test_sell_not_held(settings):
    with pytest.raises(RiskViolation, match="做空"):
        validate_sell("X.KL", 100, {}, 100)


def test_sell_more_than_held(settings):
    with pytest.raises(RiskViolation, match="超过"):
        validate_sell("X.KL", 200, {"X.KL": Position("X.KL", 100, 1.0, D)}, 100)


def test_sell_odd_lot(settings):
    with pytest.raises(RiskViolation, match="整手"):
        validate_sell("X.KL", 50, {"X.KL": Position("X.KL", 100, 1.0, D)}, 100)


# ------------------------------------------------------------ 止损


def test_stop_loss():
    pos = Position("X.KL", 100, 10.0, D)
    assert stop_loss_triggered(pos, 9.20, 0.08)       # 刚好 -8%
    assert stop_loss_triggered(pos, 9.00, 0.08)
    assert not stop_loss_triggered(pos, 9.21, 0.08)
