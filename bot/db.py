"""数据库读写。

- InMemoryDB：本地 / --dry-run / 测试用，不写任何真实数据库。
- SupabaseDB：正式运行用，通过 Supabase 的 REST API（PostgREST）读写。

两者提供相同的方法。买入 / 卖出用 execute_buy / execute_sell 完成：
“改现金、改持仓、写交易记录、删除挂单”是同一个 transaction，
Supabase 那边由 sql/schema.sql 里的 PostgreSQL 函数保证一起成功或一起失败。

密钥只从参数传入（来自环境变量），任何错误讯息都不会包含密钥。
"""
from __future__ import annotations

import copy
import logging
from contextlib import contextmanager
from datetime import date, datetime
from typing import Any, Iterator, Optional

from bot.models import Account, BotRun, DailySnapshot, PendingOrder, Position, Trade

log = logging.getLogger(__name__)

PAUSED_HINT = "数据库连不上，可能被暂停，请到 Supabase Dashboard 打开项目，点 Resume（恢复）"


class DatabaseError(Exception):
    pass


class DatabaseUnavailableError(DatabaseError):
    """连不上数据库（网络问题，或者 Supabase 项目被暂停）。"""


# ====================================================================== 内存版


class InMemoryDB:
    def __init__(self, initial_cash: float):
        self.account = Account(cash=round(initial_cash, 2), initial_cash=initial_cash)
        self.positions: dict[str, Position] = {}
        self.trades: list[Trade] = []
        self.pending_orders: list[PendingOrder] = []
        self.snapshots: list[DailySnapshot] = []
        self.runs: dict[date, BotRun] = {}
        self._next_id = 1

    @contextmanager
    def _transaction(self) -> Iterator[None]:
        backup = copy.deepcopy(
            (self.account, self.positions, self.trades, self.pending_orders, self._next_id)
        )
        try:
            yield
        except BaseException:
            (self.account, self.positions, self.trades, self.pending_orders,
             self._next_id) = backup
            raise

    def _new_id(self) -> int:
        i = self._next_id
        self._next_id += 1
        return i

    # ---------------------------------------------------------- account
    def ensure_account(self, initial_cash: float) -> Account:
        return self.get_account()

    def get_account(self) -> Account:
        return copy.copy(self.account)

    def set_cash(self, cash: float) -> None:  # 只给测试用来布置情境
        self.account.cash = round(cash, 2)

    # ---------------------------------------------------------- positions
    def get_positions(self) -> dict[str, Position]:
        return {t: copy.copy(p) for t, p in self.positions.items()}

    def upsert_position(self, pos: Position) -> None:  # 只给测试用来布置情境
        self.positions[pos.ticker] = copy.copy(pos)

    # ---------------------------------------------------------- 交易（transaction）
    def execute_buy(self, ticker: str, quantity: int, price: float, fees: float,
                    trade_date: date, reason: str, order_id: Optional[int] = None) -> Trade:
        with self._transaction():
            cost = round(quantity * price + fees, 2)
            if self.account.cash - cost < 0:
                raise DatabaseError("cash would become negative")
            self.account.cash = round(self.account.cash - cost, 2)
            self.account.updated_at = datetime.now()
            pos = self.positions.get(ticker)
            if pos:
                total = pos.quantity + quantity
                avg = round((pos.avg_price * pos.quantity + price * quantity) / total, 4)
                self.positions[ticker] = Position(ticker, total, avg, pos.buy_date)
            else:
                self.positions[ticker] = Position(ticker, quantity, round(price, 4), trade_date)
            trade = self._insert_trade(Trade(trade_date, ticker, "BUY", quantity, price, fees, reason))
            if order_id is not None:
                self.delete_pending_order(order_id)
        return trade

    def execute_sell(self, ticker: str, quantity: int, price: float, fees: float,
                     trade_date: date, reason: str, order_id: Optional[int] = None) -> Trade:
        with self._transaction():
            pos = self.positions.get(ticker)
            if pos is None:
                raise DatabaseError(f"no position in {ticker}")
            if quantity > pos.quantity:
                raise DatabaseError(f"sell {quantity} > held {pos.quantity}")
            self.account.cash = round(self.account.cash + quantity * price - fees, 2)
            self.account.updated_at = datetime.now()
            if quantity == pos.quantity:
                del self.positions[ticker]
            else:
                self.positions[ticker] = Position(ticker, pos.quantity - quantity,
                                                  pos.avg_price, pos.buy_date)
            trade = self._insert_trade(Trade(trade_date, ticker, "SELL", quantity, price, fees, reason))
            if order_id is not None:
                self.delete_pending_order(order_id)
        return trade

    def _insert_trade(self, trade: Trade) -> Trade:
        t = copy.copy(trade)
        t.id = self._new_id()
        self.trades.append(t)
        return copy.copy(t)

    def get_trades(self) -> list[Trade]:
        return [copy.copy(t) for t in self.trades]

    # ---------------------------------------------------------- pending orders
    def get_pending_orders(self) -> list[PendingOrder]:
        return [copy.copy(o) for o in sorted(self.pending_orders, key=lambda o: o.id or 0)]

    def add_pending_order(self, order: PendingOrder) -> PendingOrder:
        o = copy.copy(order)
        o.id = self._new_id()
        self.pending_orders.append(o)
        return copy.copy(o)

    def delete_pending_order(self, order_id: int) -> None:
        before = len(self.pending_orders)
        self.pending_orders = [o for o in self.pending_orders if o.id != order_id]
        if len(self.pending_orders) == before:
            raise DatabaseError(f"pending order {order_id} not found")

    # ---------------------------------------------------------- snapshots
    def save_snapshot(self, snap: DailySnapshot) -> None:
        self.snapshots = [s for s in self.snapshots if s.snapshot_date != snap.snapshot_date]
        self.snapshots.append(copy.copy(snap))
        self.snapshots.sort(key=lambda s: s.snapshot_date)

    def get_snapshots(self) -> list[DailySnapshot]:
        return [copy.copy(s) for s in self.snapshots]

    # ---------------------------------------------------------- latest prices
    def save_latest_prices(self, prices: dict[str, float], price_date: date) -> None:
        if not hasattr(self, "latest_prices"):
            self.latest_prices: dict[str, tuple[float, date]] = {}
        for t, p in prices.items():
            self.latest_prices[t] = (p, price_date)

    # ---------------------------------------------------------- bot runs
    def has_successful_run(self, run_date: date) -> bool:
        r = self.runs.get(run_date)
        return r is not None and r.status == "success"

    def record_run(self, run: BotRun) -> None:
        """同一天只有一笔（run_date UNIQUE）；已成功的不覆盖，failed / skipped 会被覆盖。"""
        if self.has_successful_run(run.run_date):
            return
        self.runs[run.run_date] = copy.copy(run)

    def ping(self) -> bool:
        return True


# ====================================================================== Supabase 版


def _d(value: Any) -> date:
    return date.fromisoformat(str(value)[:10])


class SupabaseDB:
    """通过 PostgREST（https://<project>.supabase.co/rest/v1）读写。"""

    def __init__(self, url: str, service_key: str, session=None, timeout: float = 20.0):
        import requests

        self.base = url.rstrip("/") + "/rest/v1"
        self.timeout = timeout
        self.session = session or requests.Session()
        headers = {"apikey": service_key, "Content-Type": "application/json"}
        if not service_key.startswith("sb_"):  # 旧版 JWT key 还要放 Authorization
            headers["Authorization"] = f"Bearer {service_key}"
        self.session.headers.update(headers)

    # ---------------------------------------------------------- HTTP
    def _request(self, method: str, path: str, *, params=None, json=None,
                 prefer: Optional[str] = None) -> Any:
        import requests

        headers = {"Prefer": prefer} if prefer else {}
        try:
            resp = self.session.request(method, f"{self.base}/{path}", params=params,
                                        json=json, headers=headers, timeout=self.timeout)
        except (requests.ConnectionError, requests.Timeout) as e:
            raise DatabaseUnavailableError(f"{PAUSED_HINT}（{type(e).__name__}）") from None
        if resp.status_code >= 500:
            raise DatabaseUnavailableError(f"{PAUSED_HINT}（HTTP {resp.status_code}）")
        if resp.status_code >= 400:
            detail = resp.text[:300]
            if resp.status_code in (401, 403):
                detail += "（key 可能填错，或者用了只读的 anon / publishable key）"
            raise DatabaseError(f"Supabase {method} {path} 失败：HTTP {resp.status_code} {detail}")
        if not resp.content:
            return None
        return resp.json()

    def _rpc(self, fn: str, args: dict) -> Any:
        return self._request("POST", f"rpc/{fn}", json=args)

    # ---------------------------------------------------------- account
    def ensure_account(self, initial_cash: float) -> Account:
        """第一次运行时建立账户（现金 = initial_cash）；之后直接读取。"""
        rows = self._request("GET", "account", params={"id": "eq.1", "select": "*"})
        if not rows:
            self._request("POST", "account", prefer="return=minimal",
                          json={"id": 1, "cash": initial_cash, "initial_cash": initial_cash})
            log.info("已建立账户，初始资金 RM %.2f", initial_cash)
        return self.get_account()

    def get_account(self) -> Account:
        rows = self._request("GET", "account", params={"id": "eq.1", "select": "*"})
        if not rows:
            raise DatabaseError("account 表没有资料")
        r = rows[0]
        return Account(float(r["cash"]), float(r["initial_cash"]),
                       datetime.fromisoformat(r["updated_at"]) if r.get("updated_at") else None)

    # ---------------------------------------------------------- positions
    def get_positions(self) -> dict[str, Position]:
        rows = self._request("GET", "positions", params={"select": "*", "order": "ticker"})
        return {
            r["ticker"]: Position(r["ticker"], int(r["quantity"]), float(r["avg_price"]),
                                  _d(r["buy_date"]))
            for r in rows or []
        }

    # ---------------------------------------------------------- 交易（transaction 在数据库函数里）
    def _trade_args(self, ticker, quantity, price, fees, trade_date, reason, order_id):
        return {"p_ticker": ticker, "p_quantity": int(quantity), "p_price": float(price),
                "p_fees": float(fees), "p_trade_date": str(trade_date),
                "p_reason": reason, "p_order_id": order_id}

    def _to_trade(self, r: dict) -> Trade:
        return Trade(_d(r["trade_date"]), r["ticker"], r["side"], int(r["quantity"]),
                     float(r["price"]), float(r["fees"]), r["reason"], r["id"])

    def execute_buy(self, ticker, quantity, price, fees, trade_date, reason, order_id=None) -> Trade:
        r = self._rpc("execute_buy", self._trade_args(ticker, quantity, price, fees,
                                                      trade_date, reason, order_id))
        return self._to_trade(r)

    def execute_sell(self, ticker, quantity, price, fees, trade_date, reason, order_id=None) -> Trade:
        r = self._rpc("execute_sell", self._trade_args(ticker, quantity, price, fees,
                                                       trade_date, reason, order_id))
        return self._to_trade(r)

    def get_trades(self) -> list[Trade]:
        rows = self._request("GET", "trades", params={"select": "*", "order": "id"})
        return [self._to_trade(r) for r in rows or []]

    # ---------------------------------------------------------- pending orders
    def _to_order(self, r: dict) -> PendingOrder:
        return PendingOrder(r["ticker"], r["side"], int(r["quantity"]), r["reason"],
                            _d(r["signal_date"]), float(r["signal_price"]), r["id"])

    def get_pending_orders(self) -> list[PendingOrder]:
        rows = self._request("GET", "pending_orders", params={"select": "*", "order": "id"})
        return [self._to_order(r) for r in rows or []]

    def add_pending_order(self, order: PendingOrder) -> PendingOrder:
        rows = self._request("POST", "pending_orders", prefer="return=representation", json={
            "ticker": order.ticker, "side": order.side, "quantity": order.quantity,
            "reason": order.reason, "signal_date": str(order.signal_date),
            "signal_price": order.signal_price,
        })
        return self._to_order(rows[0])

    def delete_pending_order(self, order_id: int) -> None:
        rows = self._request("DELETE", "pending_orders", params={"id": f"eq.{order_id}"},
                             prefer="return=representation")
        if not rows:
            raise DatabaseError(f"pending order {order_id} not found")

    # ---------------------------------------------------------- snapshots
    def save_snapshot(self, snap: DailySnapshot) -> None:
        self._request(
            "POST", "daily_snapshots", params={"on_conflict": "snapshot_date"},
            prefer="resolution=merge-duplicates,return=minimal",
            json={"snapshot_date": str(snap.snapshot_date), "total_value": snap.total_value,
                  "cash": snap.cash, "positions_value": snap.positions_value,
                  "benchmark_close": snap.benchmark_close},
        )

    def get_snapshots(self) -> list[DailySnapshot]:
        rows = self._request("GET", "daily_snapshots",
                             params={"select": "*", "order": "snapshot_date"})
        return [
            DailySnapshot(_d(r["snapshot_date"]), float(r["total_value"]), float(r["cash"]),
                          float(r["positions_value"]), float(r["benchmark_close"]))
            for r in rows or []
        ]

    # ---------------------------------------------------------- latest prices
    def save_latest_prices(self, prices: dict[str, float], price_date: date) -> None:
        if not prices:
            return
        self._request(
            "POST", "latest_prices", params={"on_conflict": "ticker"},
            prefer="resolution=merge-duplicates,return=minimal",
            json=[{"ticker": t, "close": p, "price_date": str(price_date),
                   "updated_at": datetime.now().astimezone().isoformat()}
                  for t, p in prices.items()],
        )

    # ---------------------------------------------------------- bot runs
    def has_successful_run(self, run_date: date) -> bool:
        rows = self._request("GET", "bot_runs", params={
            "select": "id", "run_date": f"eq.{run_date}", "status": "eq.success"})
        return bool(rows)

    def record_run(self, run: BotRun) -> None:
        self._rpc("record_run", {
            "p_run_date": str(run.run_date), "p_status": run.status,
            "p_message": run.message[:1000],
            "p_started_at": run.started_at.isoformat(),
            "p_finished_at": run.finished_at.isoformat() if run.finished_at else None,
        })

    def ping(self) -> bool:
        """简单查询（保活 / 检查连线）。"""
        self._request("GET", "bot_runs", params={"select": "id", "limit": "1"})
        return True
