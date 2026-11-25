"""美股（US）：配置、费用（SEC fee 只在卖出时收）、1 股也能买、完整买卖流程。"""
from dataclasses import replace
from datetime import date, datetime, time, timezone

import pytest

from bot.config import FeeConfig, load_market
from bot.db import InMemoryDB
from bot.fees import buy_cost, calculate_fees
from bot.main import execute
from bot.risk import RiskViolation, max_buy_quantity, validate_buy
from tests.conftest import death_cross_closes, golden_cross_closes, make_df
from tests.test_main import Quiet, market, no_sleep

TUE, WED, THU = date(2026, 11, 24), date(2026, 11, 25), date(2026, 11, 26)
US_FEES = FeeConfig(brokerage_pct=0.001, brokerage_min=1.0, sec_fee_pct_on_sell=0.0000278)


def us_close_run(d: date) -> datetime:
    return datetime(d.year, d.month, d.day, 22, 30, tzinfo=timezone.utc)


@pytest.fixture
def us():
    return load_market("US")


@pytest.fixture
def us_small(us):
    """真实 US 设定，但用短均线（5/10）方便构造交叉。"""
    return replace(us, strategy=replace(us.strategy, short_window=5, long_window=10))


# ------------------------------------------------------------ 配置


def test_us_config(us):
    assert us.market == "US" and us.currency == "USD" and us.currency_symbol == "$"
    assert us.timezone == "America/New_York" and us.market_close_cutoff == time(16, 30)
    assert us.initial_cash == 200 and us.lot_size == 1 and us.benchmark == "^GSPC"
    assert us.watchlist[:2] == ("SPYM", "IAU")              # ETF 优先
    assert not any(t.endswith(".KL") for t in us.watchlist)
    assert us.risk.max_position_pct == 0.50                 # US 覆盖
    assert us.risk.min_cash_reserve_pct == 0.05             # 沿用 defaults
    assert us.fees.stamp_duty_per_1000 == 0 and us.fees.clearing_pct == 0


def test_my_config_has_no_sec_fee():
    assert load_market("MY").fees.sec_fee_pct_on_sell == 0


# ------------------------------------------------------------ 费用


def test_us_buy_has_no_sec_fee():
    f = calculate_fees(100, US_FEES, "BUY")
    assert f.brokerage == 1.0 and f.sec_fee == 0 and f.stamp_duty == 0 and f.total == 1.0


def test_us_sell_charges_sec_fee_rounded_up_to_a_cent():
    f = calculate_fees(100, US_FEES, "SELL")        # 100 × 0.00278% = $0.00278 → $0.01
    assert f.sec_fee == 0.01 and f.total == 1.01


def test_us_sec_fee_proportional_on_large_sell():
    f = calculate_fees(10_000, US_FEES, "SELL")     # $0.278 → $0.28；佣金 0.1% = $10
    assert f.brokerage == 10.0 and f.sec_fee == 0.28 and f.total == 10.28


def test_us_minimum_brokerage():
    assert calculate_fees(50, US_FEES, "BUY").brokerage == 1.0


def test_unknown_fee_item_rejected():
    import yaml

    from bot.config import DEFAULT_MARKETS_PATH, ConfigError, parse_market
    raw = yaml.safe_load(DEFAULT_MARKETS_PATH.read_text(encoding="utf-8"))
    raw["markets"]["US"]["fees"]["sec_fee_typo"] = 0.1
    with pytest.raises(ConfigError, match="不认识"):
        parse_market(raw, "US")


# ------------------------------------------------------------ 1 股也能买


def test_us_can_buy_a_single_share(us):
    validate_buy("SPYM", 1, 84.61, 200, 200, {}, us.risk, us.fees, us.lot_size)
    assert max_buy_quantity("SPYM", 84.61, 200, 200, {}, us.risk, us.fees, us.lot_size) == 1


def test_us_quantity_respects_50pct_cap_and_reserve(us):
    # 50% × 200 = $100 上限 → F @ $11.95 最多 8 股（$95.60 + $1 佣金）
    assert max_buy_quantity("F", 11.95, 200, 200, {}, us.risk, us.fees, us.lot_size) == 8
    # 现金只剩 $50：保留 5% × 200 = $10 → 可用 $40 - 佣金 → 3 股
    assert max_buy_quantity("F", 11.95, 50, 200, {}, us.risk, us.fees, us.lot_size) == 3


def test_us_too_expensive_is_rejected_not_crashed(us):
    with pytest.raises(RiskViolation):
        validate_buy("SPY", 1, 718.78, 200, 200, {}, us.risk, us.fees, us.lot_size)
    assert max_buy_quantity("SPY", 718.78, 200, 200, {}, us.risk, us.fees, us.lot_size) == 0


# ------------------------------------------------------------ 完整流程


def test_us_full_cycle_buy_then_sell_with_sec_fee(us_small):
    st = us_small
    db, n = InMemoryDB(st.initial_cash, "US"), Quiet()
    gc = [x * 3 for x in golden_cross_closes(n_flat=29)]          # 约 $30 → $36
    r1 = execute(st, db, n, now=us_close_run(TUE),
                 downloader=market(st, TUE, {"HPE": make_df(gc, TUE)}), sleep=no_sleep)
    assert r1.status == "success"
    [order] = db.get_pending_orders()
    assert order.ticker == "HPE" and order.side == "BUY" and order.quantity >= 1

    nxt = make_df(gc + [gc[-1]], WED)
    nxt.loc[WED, "Open"] = 36.0
    r2 = execute(st, db, n, now=us_close_run(WED),
                 downloader=market(st, WED, {"HPE": nxt}), sleep=no_sleep)
    [buy] = r2.trades
    assert buy.price == 36.0 and buy.fees == 1.0                  # 买入没有 SEC fee
    assert any(m.startswith("🇺🇸 [US] 🟢 BUY") and "@ $36.00" in m for m in n.sent)

    dc = [x * 3 for x in death_cross_closes(n_flat=29)]
    sell_day = make_df(dc, WED)
    db.runs.pop(WED)                                              # 只为了在同一天造出卖出信号
    execute(st, db, n, now=us_close_run(WED), downloader=market(st, WED, {"HPE": sell_day}),
            sleep=no_sleep)
    [sell_order] = db.get_pending_orders()
    assert sell_order.side == "SELL"
    fill = make_df(dc + [dc[-1]], THU)
    fill.loc[THU, "Open"] = 30.0
    r3 = execute(st, db, n, now=us_close_run(THU),
                 downloader=market(st, THU, {"HPE": fill}), sleep=no_sleep)
    [sell] = r3.trades
    expected = calculate_fees(sell.quantity * 30.0, st.fees, "SELL")
    assert sell.fees == expected.total and expected.sec_fee >= 0.01   # 卖出有 SEC fee
    cash_after = db.get_account().cash
    assert cash_after == round(200 - buy_cost(buy.quantity, 36.0, st.fees)
                               + sell.quantity * 30.0 - expected.total, 2)
    assert db.get_positions() == {}
