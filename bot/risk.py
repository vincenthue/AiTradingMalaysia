"""风控规则（PROJECT_SPEC 第 7.3 节）+ 费用计算 + 买入数量计算 + 止损。

- 买入后现金不能低于 总资产 × min_cash_reserve_pct（当然也不能变负数）
- 只能整手买卖
- 不能卖出没有持有的股票（不准做空）
- 单只股票不能超过 max_position_pct；持仓数不能超过 max_positions
- 买卖都要计入全部费用
"""
from __future__ import annotations

import math
from dataclasses import dataclass

from bot.config import FeeConfig, RiskConfig
from bot.models import Position


class RiskViolation(Exception):
    """交易被风控拒绝。"""


@dataclass(frozen=True)
class Fees:
    brokerage: float
    clearing: float
    stamp_duty: float

    @property
    def total(self) -> float:
        return round(self.brokerage + self.clearing + self.stamp_duty, 2)


def calculate_fees(value: float, cfg: FeeConfig) -> Fees:
    """交易费用（买卖都收）。印花税按比例计算，有上限。四舍五入到仙（0.01）。"""
    if value <= 0:
        raise ValueError("交易金额必须 > 0")
    brokerage = max(value * cfg.brokerage_pct, cfg.brokerage_min)
    clearing = value * cfg.clearing_pct
    stamp = min(value / 1000.0 * cfg.stamp_duty_per_1000, cfg.stamp_duty_max)
    return Fees(round(brokerage, 2), round(clearing, 2), round(stamp, 2))


def buy_cost(quantity: int, price: float, cfg: FeeConfig) -> float:
    """买入总成本 = 股价 × 数量 + 费用。"""
    value = quantity * price
    return round(value + calculate_fees(value, cfg).total, 2)


# ------------------------------------------------------------- 买入检查


def cash_reserve(total_assets: float, risk: RiskConfig) -> float:
    """买入后至少要保留的现金 = 该市场总资产 × min_cash_reserve_pct。"""
    return round(max(total_assets, 0.0) * risk.min_cash_reserve_pct, 2)


def validate_buy(
    ticker: str,
    quantity: int,
    price: float,
    cash: float,
    total_assets: float,
    positions: dict[str, Position],
    risk: RiskConfig,
    fees: FeeConfig,
    lot_size: int,
    current_position_value: float = 0.0,
    extra_new_positions: int = 0,
) -> None:
    """不通过就抛 RiskViolation。

    current_position_value: 这只股票现有持仓市值（加仓时用）
    extra_new_positions:   已经排队、但还没成交的新开仓数量（产生信号时用）
    """
    if quantity <= 0:
        raise RiskViolation(f"{ticker}：数量必须 > 0")
    if quantity % lot_size != 0:
        raise RiskViolation(f"{ticker}：{quantity} 股不是整手（每手 {lot_size} 股）")
    if price <= 0:
        raise RiskViolation(f"{ticker}：价格无效 {price}")

    cost = buy_cost(quantity, price, fees)
    if cash - cost < 0:
        raise RiskViolation(f"{ticker}：现金不足（需要 {cost:,.2f}，现金 {cash:,.2f}）")
    reserve = cash_reserve(total_assets, risk)
    if cash - cost < reserve - 1e-9:
        raise RiskViolation(
            f"{ticker}：买入后现金 {cash - cost:,.2f} 低于保留额 {reserve:,.2f}"
            f"（总资产的 {risk.min_cash_reserve_pct:.0%}）"
        )

    if ticker not in positions:
        if len(positions) + extra_new_positions + 1 > risk.max_positions:
            raise RiskViolation(f"{ticker}：持仓数已达上限 {risk.max_positions}")

    limit = total_assets * risk.max_position_pct
    after = current_position_value + quantity * price
    if after > limit + 1e-9:
        raise RiskViolation(
            f"{ticker}：仓位 {after:,.2f} 超过总资产 {risk.max_position_pct:.0%}（{limit:,.2f}）"
        )


def max_buy_quantity(
    ticker: str,
    price: float,
    cash: float,
    total_assets: float,
    positions: dict[str, Position],
    risk: RiskConfig,
    fees: FeeConfig,
    lot_size: int,
    current_position_value: float = 0.0,
    extra_new_positions: int = 0,
) -> int:
    """在全部风控规则下，最多能买多少股（整手）。买不了返回 0。

    目标：仓位上限 = 总资产 × max_position_pct；同时受现金（扣保留额和费用）限制。
    """
    if price <= 0:
        return 0
    lot_value = price * lot_size
    room_by_pct = total_assets * risk.max_position_pct - current_position_value
    room_by_cash = cash - cash_reserve(total_assets, risk)
    lots = int(math.floor(min(room_by_pct, room_by_cash) / lot_value + 1e-9))
    # 扣掉费用后可能多算一两手，往下调到通过全部检查为止
    while lots > 0:
        qty = lots * lot_size
        try:
            validate_buy(
                ticker, qty, price, cash, total_assets, positions, risk, fees,
                lot_size, current_position_value, extra_new_positions,
            )
            return qty
        except RiskViolation:
            lots -= 1
    return 0


# ------------------------------------------------------------- 卖出检查


def validate_sell(
    ticker: str, quantity: int, positions: dict[str, Position], lot_size: int
) -> None:
    if quantity <= 0:
        raise RiskViolation(f"{ticker}：数量必须 > 0")
    if quantity % lot_size != 0:
        raise RiskViolation(f"{ticker}：{quantity} 股不是整手（每手 {lot_size} 股）")
    pos = positions.get(ticker)
    if pos is None or pos.quantity <= 0:
        raise RiskViolation(f"{ticker}：没有持有，不能卖出（不准做空）")
    if quantity > pos.quantity:
        raise RiskViolation(f"{ticker}：卖出 {quantity} 股超过持有的 {pos.quantity} 股")


# ------------------------------------------------------------- 止损


def stop_loss_triggered(position: Position, close: float, stop_loss_pct: float) -> bool:
    """收盘价 <= 平均买入价 × (1 - stop_loss_pct) → 触发止损。"""
    return close <= position.avg_price * (1 - stop_loss_pct) + 1e-9
