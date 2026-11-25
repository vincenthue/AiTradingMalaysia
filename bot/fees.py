"""交易费用：每个市场在 markets.yaml 的 fees 里列出自己要收的项目，这里按项目计算。

没有 `if market == ...`：费用由“项目”组成，每个项目是一个函数（FEE_COMPONENTS）。
市场没写的项目，参数就是 0 → 不收。要加新的费用种类，只要在这里加一个项目函数，
再在需要的市场的 fees 里写上参数。

- brokerage   佣金：买卖都收，按比例、有最低收费
- clearing    清算费（马股）：买卖都收，按比例
- stamp_duty  印花税（马股）：买卖都收，每 1000 收多少，有上限
- sec_fee     SEC fee（美股）：只在卖出时收，按比例，进位到 1 分钱（和券商一样）

金额四舍五入到 0.01（仙 / 分）；SEC fee 是无条件进位。
"""
from __future__ import annotations

import math
from dataclasses import dataclass, fields
from typing import Callable

from bot.config import FeeConfig

SIDES = ("BUY", "SELL")


def _cents(x: float) -> float:
    return round(x, 2)


def _ceil_cents(x: float) -> float:
    if x <= 0:
        return 0.0
    return math.ceil(round(x * 100, 6)) / 100


def _brokerage(value: float, side: str, c: FeeConfig) -> float:
    if c.brokerage_pct <= 0 and c.brokerage_min <= 0:
        return 0.0
    return _cents(max(value * c.brokerage_pct, c.brokerage_min))


def _clearing(value: float, side: str, c: FeeConfig) -> float:
    return _cents(value * c.clearing_pct)


def _stamp_duty(value: float, side: str, c: FeeConfig) -> float:
    duty = value / 1000.0 * c.stamp_duty_per_1000
    if c.stamp_duty_max is not None:
        duty = min(duty, c.stamp_duty_max)
    return _cents(duty)


def _sec_fee(value: float, side: str, c: FeeConfig) -> float:
    return _ceil_cents(value * c.sec_fee_pct_on_sell) if side == "SELL" else 0.0


# 项目名称 → 计算函数（名称要和 Fees 的栏位一样）
FEE_COMPONENTS: dict[str, Callable[[float, str, FeeConfig], float]] = {
    "brokerage": _brokerage,
    "clearing": _clearing,
    "stamp_duty": _stamp_duty,
    "sec_fee": _sec_fee,
}


@dataclass(frozen=True)
class Fees:
    brokerage: float = 0.0
    clearing: float = 0.0
    stamp_duty: float = 0.0
    sec_fee: float = 0.0

    @property
    def total(self) -> float:
        return round(sum(getattr(self, f.name) for f in fields(self)), 2)


def calculate_fees(value: float, cfg: FeeConfig, side: str) -> Fees:
    """一笔交易的全部费用。side 是 BUY 或 SELL（有些费用只在卖出时收）。"""
    if value <= 0:
        raise ValueError("交易金额必须 > 0")
    if side not in SIDES:
        raise ValueError(f"side 必须是 BUY 或 SELL，现在是 {side!r}")
    return Fees(**{name: fn(value, side, cfg) for name, fn in FEE_COMPONENTS.items()})


def buy_cost(quantity: int, price: float, cfg: FeeConfig) -> float:
    """买入总成本 = 股价 × 数量 + 买入费用。"""
    value = quantity * price
    return round(value + calculate_fees(value, cfg, "BUY").total, 2)
