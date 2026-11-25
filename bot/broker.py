"""模拟券商：执行虚拟买卖、计算费用。

只是数据库里的记录，没有任何真实券商 / 真实下单 / 真实资金。

挂单（pending order）在下一个交易日用开盘价成交：
- 先执行全部 SELL（释放现金），再按挂单顺序执行 BUY
- BUY 成交前用开盘价重新跑风控；不够的话减少手数，减到 0 就取消
- SELL：没有持有就取消（不准做空）
- 该股今天没有可用数据（缺数据 / 数据可疑）→ 挂单保留，今天不执行
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date

from bot.config import Settings
from bot.models import PendingOrder, Position, Trade
from bot.risk import (  # noqa: F401 - calculate_fees / Fees 从这里对外提供
    Fees,
    RiskViolation,
    calculate_fees,
    max_buy_quantity,
    validate_sell,
)

log = logging.getLogger(__name__)


@dataclass
class ExecutionResult:
    trades: list[Trade] = field(default_factory=list)
    cancelled: list[tuple[PendingOrder, str]] = field(default_factory=list)
    reduced: list[tuple[PendingOrder, int, str]] = field(default_factory=list)  # (挂单, 实际数量, 原因)
    deferred: list[PendingOrder] = field(default_factory=list)


class SimBroker:
    def __init__(self, db, settings: Settings):
        self.db = db
        self.st = settings

    # ------------------------------------------------------------ 单笔交易
    # “改现金、改持仓、写交易、删挂单”由 db.execute_buy / execute_sell 在同一个 transaction 完成
    def buy(self, ticker: str, quantity: int, price: float, trade_date: date,
            reason: str, order_id: int | None = None) -> Trade:
        if quantity <= 0 or quantity % self.st.lot_size != 0:
            raise RiskViolation(f"{ticker}：{quantity} 股不是整手")
        fees = calculate_fees(quantity * price, self.st.fees).total
        if self.db.get_account().cash - (quantity * price + fees) < 0:
            raise RiskViolation(f"{ticker}：现金会变负数，拒绝成交")
        return self.db.execute_buy(ticker, quantity, price, fees, trade_date, reason, order_id)

    def sell(self, ticker: str, quantity: int, price: float, trade_date: date,
             reason: str, order_id: int | None = None) -> Trade:
        validate_sell(ticker, quantity, self.db.get_positions(), self.st.lot_size)
        fees = calculate_fees(quantity * price, self.st.fees).total
        return self.db.execute_sell(ticker, quantity, price, fees, trade_date, reason, order_id)

    # ------------------------------------------------------------ 执行挂单
    def execute_pending(
        self,
        open_prices: dict[str, float],
        trade_date: date,
        valuation_prices: dict[str, float] | None = None,
    ) -> ExecutionResult:
        """用今天的开盘价执行挂单。

        open_prices: 今天有可用数据的股票 → 开盘价（不在里面的股票，挂单保留）
        valuation_prices: 计算总资产用的价格（默认用开盘价；没有的话用平均成本）
        """
        res = ExecutionResult()
        orders = self.db.get_pending_orders()
        sells = [o for o in orders if o.side == "SELL"]
        buys = [o for o in orders if o.side == "BUY"]

        for o in sells:
            price = open_prices.get(o.ticker)
            if price is None:
                res.deferred.append(o)
                continue
            held = self.db.get_positions().get(o.ticker)
            if held is None:
                self._cancel(o, "没有持有，不能卖出（不准做空）", res)
                continue
            qty = min(o.quantity, held.quantity)
            res.trades.append(self.sell(o.ticker, qty, price, trade_date, o.reason, o.id))

        for o in buys:
            price = open_prices.get(o.ticker)
            if price is None:
                res.deferred.append(o)
                continue
            positions = self.db.get_positions()
            cash = self.db.get_account().cash
            total = self.total_assets(cash, positions, valuation_prices or open_prices)
            cur_val = positions[o.ticker].quantity * price if o.ticker in positions else 0.0
            allowed = max_buy_quantity(
                o.ticker, price, cash, total, positions, self.st.risk, self.st.fees,
                self.st.lot_size, current_position_value=cur_val,
            )
            qty = min(o.quantity, allowed)
            if qty <= 0:
                self._cancel(o, f"按开盘价 RM {price:.2f} 重新检查风控后买不了（现金或仓位上限）", res)
                continue
            if qty < o.quantity:
                res.reduced.append((o, qty, f"开盘价 RM {price:.2f}，由 {o.quantity} 股减为 {qty} 股"))
            res.trades.append(self.buy(o.ticker, qty, price, trade_date, o.reason, o.id))
        return res

    def _cancel(self, order: PendingOrder, why: str, res: ExecutionResult) -> None:
        self.db.delete_pending_order(order.id)
        res.cancelled.append((order, why))
        log.info("取消挂单 %s %s：%s", order.side, order.ticker, why)

    # ------------------------------------------------------------ 估值
    @staticmethod
    def positions_value(positions: dict[str, Position], prices: dict[str, float]) -> float:
        return round(
            sum(p.quantity * prices.get(t, p.avg_price) for t, p in positions.items()), 2
        )

    @classmethod
    def total_assets(cls, cash: float, positions: dict[str, Position],
                     prices: dict[str, float]) -> float:
        return round(cash + cls.positions_value(positions, prices), 2)
