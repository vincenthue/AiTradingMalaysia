"""数据库读写。

- InMemoryDB：本地 / --dry-run / 测试用，不写任何真实数据库。
- SupabaseDB：正式运行用，通过 Supabase 的 REST API（PostgREST）读写。

两者提供相同的方法，而且每个实例只属于一个市场（market）：
所有读写都只碰该市场的数据，一个市场的现金不能拿去买另一个市场的股票。

买入 / 卖出用 execute_buy / execute_sell 完成：
“改现金、改持仓、写交易记录、删除挂单”是同一个 transaction，
Supabase 那边由数据库里的 PostgreSQL 函数保证一起成功或一起失败（见 sql/）。

密钥只从参数传入（来自环境变量），任何错误讯息都不会包含密钥。
"""
from __future__ import annotations

import copy
import logging
from contextlib import contextmanager
from dataclasses import dataclass, field
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


@dataclass
class _MarketState:
    account: Account
    positions: dict = field(default_factory=dict)
    trades: list = field(default_factory=list)
    pending_orders: list = field(default_factory=list)
    snapshots: list = field(default_factory=list)
    runs: dict = field(default_factory=dict)
    latest_prices: dict = field(default_factory=dict)


class InMemoryStore:
    """内存版的“整个数据库”：每个市场一份独立的数据；id 在所有市场之间共用（像数据库的 identity）。"""

    def __init__(self):
        self.markets: dict[str, _MarketState] = {}
        self.next_id = 1


class InMemoryDB:
    """某一个市场在 InMemoryStore 里的视图。多个市场可以共用同一个 store（测试隔离用）。"""

    def __init__(self, initial_cash: float, market: str = "MY",
                 store: Optional[InMemoryStore] = None):
        self.market = market
        self.store = store or InMemoryStore()
        if market not in self.store.markets:
            self.store.markets[market] = _MarketState(
                Account(cash=round(initial_cash, 2), initial_cash=initial_cash))

    # 该市场的数据（属性名称和以前一样，测试可以直接看）
    @property
    def _s(self) -> _MarketState:
        return self.store.markets[self.market]

    account = property(lambda self: self._s.account,
                       lambda self, v: setattr(self._s, "account", v))
    positions = property(lambda self: self._s.positions,
                         lambda self, v: setattr(self._s, "positions", v))
    trades = property(lambda self: self._s.trades,
                      lambda self, v: setattr(self._s, "trades", v))
    pending_orders = property(lambda self: self._s.pending_orders,
                              lambda self, v: setattr(self._s, "pending_orders", v))
    snapshots = property(lambda self: self._s.snapshots,
                         lambda self, v: setattr(self._s, "snapshots", v))
    runs = property(lambda self: self._s.runs)
    latest_prices = property(lambda self: self._s.latest_prices)

    @contextmanager
    def _transaction(self) -> Iterator[None]:
        backup = copy.deepcopy((self._s, self.store.next_id))
        try:
            yield
        except BaseException:
            self.store.markets[self.market], self.store.next_id = backup
            raise

    def _new_id(self) -> int:
        i = self.store.next_id
        self.store.next_id += 1
        return i

    # ---------------------------------------------------------- account
    def ensure_account(self, initial_cash: float, currency: str = "") -> Account:
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
        for t, p in prices.items():
            self.latest_prices[t] = (p, price_date)

    # ---------------------------------------------------------- bot runs
    def has_successful_run(self, run_date: date) -> bool:
        r = self.runs.get(run_date)
        return r is not None and r.status == "success"

    def get_run(self, run_date: date) -> Optional[BotRun]:
        r = self.runs.get(run_date)
        return copy.copy(r) if r else None

    def last_success_date(self) -> Optional[date]:
        done = [d for d, r in self.runs.items() if r.status == "success"]
        return max(done) if done else None

    def record_run(self, run: BotRun) -> None:
        """同一市场同一天只有一笔（(market, run_date) UNIQUE）；已成功的不覆盖，failed / skipped 会被覆盖。"""
        if self.has_successful_run(run.run_date):
            return
        self.runs[run.run_date] = copy.copy(run)

    def ping(self) -> bool:
        return True


# ====================================================================== Supabase 版


def _d(value: Any) -> date:
    return date.fromisoformat(str(value)[:10])


class SupabaseDB:
    """通过 PostgREST（https://<project>.supabase.co/rest/v1）读写某一个市场的数据。

    market=None 只能用来 ping（保活）；其他方法都必须先指定市场。
    """

    def __init__(self, url: str, service_key: str, market: Optional[str] = None,
                 session=None, timeout: float = 20.0):
        import requests

        self.market = market
        self.base = url.rstrip("/") + "/rest/v1"
        self.timeout = timeout
        self.session = session or requests.Session()
        headers = {"apikey": service_key, "Content-Type": "application/json"}
        if not service_key.startswith("sb_"):  # 旧版 JWT key 还要放 Authorization
            headers["Authorization"] = f"Bearer {service_key}"
        self.session.headers.update(headers)

    @property
    def _m(self) -> str:
        if not self.market:
            raise DatabaseError("没有指定市场（market），拒绝读写")
        return self.market

    def _where(self, **params) -> dict:
        """所有查询都加上 market=eq.<市场>。"""
        return {"market": f"eq.{self._m}", **params}

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
        return self._request("POST", f"rpc/{fn}", json={**args, "p_market": self._m})

    PAGE_SIZE = 1000   # Supabase 每次查询最多回传 1000 行

    def _get_all(self, path: str, params: dict) -> list:
        """分批读取（每批 PAGE_SIZE 行）直到读完，避免超过 1000 行时只拿到前 1000 行。"""
        rows: list = []
        while True:
            page = self._request("GET", path, params={**params, "limit": str(self.PAGE_SIZE),
                                                      "offset": str(len(rows))}) or []
            rows.extend(page)
            if len(page) < self.PAGE_SIZE:
                return rows

    # ---------------------------------------------------------- account
    def ensure_account(self, initial_cash: float, currency: str = "") -> Account:
        """该市场第一次运行时建立账户（现金 = initial_cash）；之后直接读取。"""
        rows = self._request("GET", "accounts", params=self._where(select="market"))
        if not rows:
            if not currency:
                raise DatabaseError(f"建立 {self._m} 账户需要货币代码")
            self._request("POST", "accounts", prefer="return=minimal",
                          json={"market": self._m, "currency": currency,
                                "cash": initial_cash, "initial_cash": initial_cash})
            log.info("已建立 %s 账户，初始资金 %s %.2f", self._m, currency, initial_cash)
        return self.get_account()

    def get_account(self) -> Account:
        rows = self._request("GET", "accounts", params=self._where(select="*"))
        if not rows:
            raise DatabaseError(f"accounts 表没有 {self._m} 的账户")
        r = rows[0]
        return Account(float(r["cash"]), float(r["initial_cash"]),
                       datetime.fromisoformat(r["updated_at"]) if r.get("updated_at") else None)

    # ---------------------------------------------------------- positions
    def get_positions(self) -> dict[str, Position]:
        rows = self._get_all("positions", self._where(select="*", order="ticker"))
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
        if r.get("market") not in (None, self._m):
            raise DatabaseError(f"交易记录的市场 {r.get('market')} 和 {self._m} 不一致")
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
        rows = self._get_all("trades", self._where(select="*", order="id"))
        return [self._to_trade(r) for r in rows or []]

    # ---------------------------------------------------------- pending orders
    def _to_order(self, r: dict) -> PendingOrder:
        return PendingOrder(r["ticker"], r["side"], int(r["quantity"]), r["reason"],
                            _d(r["signal_date"]), float(r["signal_price"]), r["id"])

    def get_pending_orders(self) -> list[PendingOrder]:
        rows = self._get_all("pending_orders", self._where(select="*", order="id"))
        return [self._to_order(r) for r in rows or []]

    def add_pending_order(self, order: PendingOrder) -> PendingOrder:
        rows = self._request("POST", "pending_orders", prefer="return=representation", json={
            "market": self._m, "ticker": order.ticker, "side": order.side,
            "quantity": order.quantity, "reason": order.reason,
            "signal_date": str(order.signal_date), "signal_price": order.signal_price,
        })
        return self._to_order(rows[0])

    def delete_pending_order(self, order_id: int) -> None:
        rows = self._request("DELETE", "pending_orders",
                             params=self._where(id=f"eq.{order_id}"),
                             prefer="return=representation")
        if not rows:
            raise DatabaseError(f"pending order {order_id} not found in market {self._m}")

    # ---------------------------------------------------------- snapshots
    def save_snapshot(self, snap: DailySnapshot) -> None:
        self._request(
            "POST", "daily_snapshots", params={"on_conflict": "market,snapshot_date"},
            prefer="resolution=merge-duplicates,return=minimal",
            json={"market": self._m, "snapshot_date": str(snap.snapshot_date),
                  "total_value": snap.total_value, "cash": snap.cash,
                  "positions_value": snap.positions_value,
                  "benchmark_close": snap.benchmark_close},
        )

    def get_snapshots(self) -> list[DailySnapshot]:
        rows = self._get_all("daily_snapshots", self._where(select="*", order="snapshot_date"))
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
            "POST", "latest_prices", params={"on_conflict": "market,ticker"},
            prefer="resolution=merge-duplicates,return=minimal",
            json=[{"market": self._m, "ticker": t, "close": p, "price_date": str(price_date),
                   "updated_at": datetime.now().astimezone().isoformat()}
                  for t, p in prices.items()],
        )

    # ---------------------------------------------------------- bot runs
    def has_successful_run(self, run_date: date) -> bool:
        rows = self._request("GET", "bot_runs", params=self._where(
            select="id", run_date=f"eq.{run_date}", status="eq.success"))
        return bool(rows)

    def last_success_date(self) -> Optional[date]:
        """该市场最近一个成功处理的交易日（没有就 None）。"""
        rows = self._request("GET", "bot_runs", params=self._where(
            select="run_date", status="eq.success", order="run_date.desc", limit="1"))
        return _d(rows[0]["run_date"]) if rows else None

    def get_run(self, run_date: date) -> Optional[BotRun]:
        """该市场某一天的运行记录（没有就 None）。"""
        rows = self._request("GET", "bot_runs", params=self._where(
            select="run_date,status,message,started_at,finished_at", run_date=f"eq.{run_date}"))
        if not rows:
            return None
        r = rows[0]
        return BotRun(_d(r["run_date"]), r["status"], r["message"],
                      datetime.fromisoformat(r["started_at"]),
                      datetime.fromisoformat(r["finished_at"]) if r.get("finished_at") else None)

    def record_run(self, run: BotRun) -> None:
        self._rpc("record_run", {
            "p_run_date": str(run.run_date), "p_status": run.status,
            "p_message": run.message[:1000],
            "p_started_at": run.started_at.isoformat(),
            "p_finished_at": run.finished_at.isoformat() if run.finished_at else None,
        })

    def ping(self) -> bool:
        """简单查询（保活 / 检查连线）。不需要指定市场。"""
        self._request("GET", "bot_runs", params={"select": "id", "limit": "1"})
        return True
