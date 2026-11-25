from datetime import date

import pytest

from bot.broker import SimBroker
from bot.db import InMemoryDB
from bot.models import PendingOrder, Position
from bot.risk import RiskViolation

D0 = date(2026, 11, 23)
D1 = date(2026, 11, 24)


@pytest.fixture
def db(settings):
    return InMemoryDB(settings.initial_cash)


@pytest.fixture
def broker(db, settings):
    return SimBroker(db, settings)


def test_buy_and_sell_update_cash_positions_trades(db, broker):
    broker.buy("X.KL", 100, 10.0, D0, "test")
    assert db.get_account().cash == round(10000 - 1000 - 9.30, 2)
    assert db.get_positions()["X.KL"].quantity == 100
    broker.sell("X.KL", 100, 11.0, D1, "test")
    # 卖 RM1,100：费用 8 + 0.33 + 1.10 = 9.43
    assert db.get_account().cash == round(10000 - 1009.30 + 1100 - 9.43, 2)
    assert db.get_positions() == {}
    assert [t.side for t in db.get_trades()] == ["BUY", "SELL"]


def test_sell_not_held_rejected(broker, db):
    with pytest.raises(RiskViolation):
        broker.sell("X.KL", 100, 10.0, D1, "test")
    assert db.get_trades() == []


def test_transaction_rolls_back_everything(db, broker):
    """删除挂单失败 → 现金、持仓、交易记录全部回滚。"""
    with pytest.raises(Exception):
        broker.buy("X.KL", 100, 10.0, D0, "test", order_id=999)  # 不存在的挂单
    assert db.get_account().cash == 10000
    assert db.get_positions() == {}
    assert db.get_trades() == []


def test_execute_pending_sells_first_then_buys(db, broker):
    db.upsert_position(Position("S.KL", 100, 10.0, D0))
    # 现金 RM 600：不先卖的话，买 RM200（+费用）后会低于 RM500 保留额
    db.set_cash(600)
    db.add_pending_order(PendingOrder("B.KL", "BUY", 100, "金叉", D0, 2.0))
    db.add_pending_order(PendingOrder("S.KL", "SELL", 100, "死叉", D0, 10.0))
    res = broker.execute_pending({"B.KL": 2.0, "S.KL": 10.0}, D1)
    assert [t.side for t in res.trades] == ["SELL", "BUY"]
    assert db.get_pending_orders() == []


def test_buy_reduced_when_open_gaps_up(db, broker):
    # 信号时以 RM 6.00 计 300 股（RM1,800），开盘跳到 RM 8.00 → 20% 上限只够 200 股
    db.add_pending_order(PendingOrder("X.KL", "BUY", 300, "金叉", D0, 6.0))
    res = broker.execute_pending({"X.KL": 8.0}, D1)
    assert res.trades[0].quantity == 200
    assert res.reduced and res.reduced[0][1] == 200


def test_buy_cancelled_when_nothing_affordable(db, broker):
    db.add_pending_order(PendingOrder("X.KL", "BUY", 100, "金叉", D0, 15.0))
    res = broker.execute_pending({"X.KL": 25.0}, D1)  # 一手 RM2,500 > 20%
    assert res.trades == [] and len(res.cancelled) == 1
    assert db.get_pending_orders() == []


def test_sell_order_without_position_cancelled(db, broker):
    db.add_pending_order(PendingOrder("X.KL", "SELL", 100, "死叉", D0, 10.0))
    res = broker.execute_pending({"X.KL": 10.0}, D1)
    assert res.trades == [] and "做空" in res.cancelled[0][1]


def test_order_deferred_when_no_price_today(db, broker):
    db.add_pending_order(PendingOrder("X.KL", "BUY", 100, "金叉", D0, 10.0))
    res = broker.execute_pending({}, D1)
    assert res.trades == [] and len(res.deferred) == 1
    assert len(db.get_pending_orders()) == 1


def test_avg_price_on_add(db, broker):
    broker.buy("X.KL", 100, 10.0, D0, "t")
    broker.buy("X.KL", 100, 12.0, D1, "t")
    pos = db.get_positions()["X.KL"]
    assert pos.quantity == 200 and pos.avg_price == 11.0 and pos.buy_date == D0
