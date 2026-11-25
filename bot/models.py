"""共用的数据结构（账户、持仓、交易、挂单、快照、运行记录）。"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from typing import Literal, Optional

Side = Literal["BUY", "SELL"]
RunStatus = Literal["success", "skipped", "failed"]


@dataclass
class Account:
    cash: float
    initial_cash: float
    updated_at: Optional[datetime] = None


@dataclass
class Position:
    ticker: str
    quantity: int
    avg_price: float      # 平均买入价（不含费用）
    buy_date: date        # 第一次买入日期


@dataclass
class Trade:
    trade_date: date
    ticker: str
    side: Side
    quantity: int
    price: float
    fees: float
    reason: str
    id: Optional[int] = None

    @property
    def value(self) -> float:
        return round(self.quantity * self.price, 2)


@dataclass
class PendingOrder:
    ticker: str
    side: Side
    quantity: int
    reason: str
    signal_date: date
    signal_price: float   # 产生信号时的收盘价（只用来估算，不用来成交）
    id: Optional[int] = None


@dataclass
class DailySnapshot:
    snapshot_date: date
    total_value: float
    cash: float
    positions_value: float
    benchmark_close: float


@dataclass
class BotRun:
    run_date: date
    status: RunStatus
    message: str
    started_at: datetime
    finished_at: Optional[datetime] = None
