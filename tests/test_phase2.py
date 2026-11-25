"""第 2 阶段：Supabase 客户端、Telegram、安全检查（全部用假的 HTTP，不连网络）。"""
import logging
from datetime import date, datetime
from zoneinfo import ZoneInfo

import pytest
import requests

from bot.db import DatabaseError, DatabaseUnavailableError, InMemoryDB, SupabaseDB
from bot.main import execute
from bot.models import BotRun, PendingOrder, Position
from bot.notifier import TelegramNotifier
from bot.safety import StateError, check_state, redact
from tests.test_main import TUE, Quiet, at, market, no_sleep

FAKE_KEY = "sb_secret_FAKEFAKEFAKEFAKEFAKE1234"
FAKE_TOKEN = "123456789:AAFakeTokenFakeTokenFakeTokenFake123"


class FakeResp:
    def __init__(self, status=200, payload=None, text=""):
        self.status_code = status
        self._payload = payload
        self.text = text or ("" if payload is None else str(payload))
        self.content = b"" if payload is None else b"x"

    def json(self):
        return self._payload


class FakeSession:
    def __init__(self, responses=None, exc=None):
        self.headers = {}
        self.calls = []
        self.responses = list(responses or [])
        self.exc = exc

    def _next(self):
        if self.exc:
            raise self.exc
        return self.responses.pop(0) if self.responses else FakeResp(200, [])

    def request(self, method, url, **kw):
        self.calls.append((method, url, kw))
        return self._next()

    def post(self, url, **kw):
        self.calls.append(("POST", url, kw))
        return self._next()


# ------------------------------------------------------------ SupabaseDB


def test_supabase_headers_new_key_no_bearer():
    s = FakeSession()
    SupabaseDB("https://abc.supabase.co/", FAKE_KEY, session=s)
    assert s.headers["apikey"] == FAKE_KEY and "Authorization" not in s.headers


def test_supabase_headers_legacy_jwt():
    s = FakeSession()
    SupabaseDB("https://abc.supabase.co", "eyJhbGciOi.payload.sig", session=s)
    assert s.headers["Authorization"].startswith("Bearer ")


def test_supabase_connection_error_means_paused():
    db = SupabaseDB("https://abc.supabase.co", FAKE_KEY, market="MY",
                    session=FakeSession(exc=requests.ConnectionError("dns fail")))
    with pytest.raises(DatabaseUnavailableError, match="Resume"):
        db.get_positions()


def test_supabase_5xx_means_unavailable():
    db = SupabaseDB("https://abc.supabase.co", FAKE_KEY, session=FakeSession([FakeResp(540, {})]))
    with pytest.raises(DatabaseUnavailableError):
        db.ping()


def test_supabase_401_hint_without_key():
    db = SupabaseDB("https://abc.supabase.co", FAKE_KEY, market="MY",
                    session=FakeSession([FakeResp(401, {"message": "Invalid API key"})]))
    with pytest.raises(DatabaseError) as ei:
        db.get_positions()
    assert "anon" in str(ei.value) and FAKE_KEY not in str(ei.value)


def test_supabase_execute_buy_calls_rpc_with_market():
    trade_row = {"id": 7, "market": "MY", "trade_date": "2026-11-24", "ticker": "1155.KL",
                 "side": "BUY", "quantity": 100, "price": 10.0, "fees": 9.3, "reason": "x"}
    s = FakeSession([FakeResp(200, trade_row)])
    db = SupabaseDB("https://abc.supabase.co", FAKE_KEY, market="MY", session=s)
    t = db.execute_buy("1155.KL", 100, 10.0, 9.3, date(2026, 11, 24), "x", order_id=3)
    method, url, kw = s.calls[0]
    assert method == "POST" and url.endswith("/rest/v1/rpc/execute_buy")
    assert kw["json"]["p_order_id"] == 3 and kw["json"]["p_market"] == "MY"
    assert t.id == 7 and t.quantity == 100


def test_supabase_rejects_trade_row_from_other_market():
    row = {"id": 7, "market": "US", "trade_date": "2026-11-24", "ticker": "F", "side": "BUY",
           "quantity": 1, "price": 10.0, "fees": 1.0, "reason": "x"}
    db = SupabaseDB("https://abc.supabase.co", FAKE_KEY, market="MY",
                    session=FakeSession([FakeResp(200, [row])]))
    with pytest.raises(DatabaseError, match="市场"):
        db.get_trades()


def test_supabase_without_market_refuses_reads_and_writes():
    db = SupabaseDB("https://abc.supabase.co", FAKE_KEY, session=FakeSession())
    with pytest.raises(DatabaseError, match="market"):
        db.get_positions()
    assert db.ping() is True   # 保活不需要市场


def test_supabase_every_query_filters_by_market():
    s = FakeSession()
    db = SupabaseDB("https://abc.supabase.co", FAKE_KEY, market="US", session=s)
    db.get_positions(); db.get_trades(); db.get_pending_orders(); db.get_snapshots()
    db.has_successful_run(date(2026, 11, 24))
    for method, url, kw in s.calls:
        assert kw["params"]["market"] == "eq.US", url


def test_supabase_writes_carry_market():
    from bot.models import BotRun, DailySnapshot
    s = FakeSession([FakeResp(201, [{"id": 1, "ticker": "F", "side": "BUY", "quantity": 1,
                                     "reason": "", "signal_date": "2026-11-24",
                                     "signal_price": 12.0}])])
    db = SupabaseDB("https://abc.supabase.co", FAKE_KEY, market="US", session=s)
    db.add_pending_order(PendingOrder("F", "BUY", 1, "", date(2026, 11, 24), 12.0))
    db.save_snapshot(DailySnapshot(date(2026, 11, 24), 200, 200, 0, 6000))
    now = datetime(2026, 11, 24, tzinfo=ZoneInfo("UTC"))
    db.record_run(BotRun(date(2026, 11, 24), "success", "ok", now, now))
    posted = [kw for m, url, kw in s.calls]
    assert posted[0]["json"]["market"] == "US"
    assert posted[1]["json"]["market"] == "US"
    assert posted[1]["params"] == {"on_conflict": "market,snapshot_date"}
    assert posted[2]["json"]["p_market"] == "US"


def test_supabase_ensure_account_creates_once_for_market():
    s = FakeSession([FakeResp(200, []), FakeResp(201, None),
                     FakeResp(200, [{"cash": 200, "initial_cash": 200, "updated_at": None}])])
    acct = SupabaseDB("https://abc.supabase.co", FAKE_KEY, market="US",
                      session=s).ensure_account(200, "USD")
    assert acct.cash == 200 and s.calls[1][0] == "POST"
    assert s.calls[1][2]["json"] == {"market": "US", "currency": "USD", "cash": 200,
                                     "initial_cash": 200}


# ------------------------------------------------------------ Telegram


def test_telegram_retries_twice_then_gives_up_without_raising(caplog):
    s = FakeSession(exc=requests.ConnectionError(f"https://api.telegram.org/bot{FAKE_TOKEN}/x"))
    sleeps = []
    n = TelegramNotifier(FAKE_TOKEN, "1137920990", session=s, sleep=sleeps.append)
    with caplog.at_level(logging.WARNING):
        assert n.send("hello") is False
    assert len(s.calls) == 3 and len(sleeps) == 2  # 1 次 + 重试 2 次
    assert FAKE_TOKEN not in caplog.text


def test_telegram_success():
    s = FakeSession([FakeResp(200, {"ok": True})])
    n = TelegramNotifier(FAKE_TOKEN, "1137920990", session=s, sleep=lambda x: None)
    assert n.send("hi") is True
    assert s.calls[0][2]["json"] == {"chat_id": "1137920990", "text": "hi"}


def test_telegram_message_redacts_secrets():
    s = FakeSession([FakeResp(200, {"ok": True})])
    n = TelegramNotifier(FAKE_TOKEN, "1", session=s, sleep=lambda x: None)
    n.send(f"error with {FAKE_TOKEN} and {FAKE_KEY}")
    sent = s.calls[0][2]["json"]["text"]
    assert FAKE_TOKEN not in sent and FAKE_KEY not in sent


def test_redact_env_values():
    assert redact("x my-secret-value y", ["my-secret-value"]) == "x *** y"


# ------------------------------------------------------------ 状态一致性


def _acct(cash):
    from bot.models import Account
    return Account(cash, 10000)


def test_state_negative_cash():
    with pytest.raises(StateError, match="负数"):
        check_state(_acct(-1), {}, [], 100)


def test_state_odd_lot_position():
    with pytest.raises(StateError, match="整数倍"):
        check_state(_acct(100), {"X": Position("X", 150, 1.0, TUE)}, [], 100)


def test_state_sell_order_without_position():
    with pytest.raises(StateError):
        check_state(_acct(100), {}, [PendingOrder("X", "SELL", 100, "", TUE, 1.0)], 100)


def test_capital_mismatch_halts_and_alerts(small_settings):
    """markets.yaml 本金和数据库不一致 → 不交易、发警报。"""
    db, n = InMemoryDB(5000), Quiet()   # 数据库是 5000，设定档是 10000
    r = execute(small_settings, db, n, now=at(TUE), downloader=market(small_settings, TUE),
                sleep=no_sleep)
    assert r.status == "failed" and r.exit_code == 1
    assert db.get_trades() == [] and db.get_snapshots() == []
    assert any("initial_cash" in m for m in n.sent)


def test_bad_state_halts_and_alerts(small_settings):
    db, n = InMemoryDB(10000), Quiet()
    db.set_cash(-5)
    r = execute(small_settings, db, n, now=at(TUE), downloader=market(small_settings, TUE),
                sleep=no_sleep)
    assert r.status == "failed" and r.exit_code == 1
    assert db.get_trades() == [] and db.get_snapshots() == []
    assert any("状态不一致" in m for m in n.sent)


def test_db_unavailable_alerts_resume(small_settings):
    class Down(InMemoryDB):
        def has_successful_run(self, d):
            raise DatabaseUnavailableError("down")

        def record_run(self, run):
            raise DatabaseUnavailableError("down")

    n = Quiet()
    r = execute(small_settings, Down(10000), n, now=at(TUE),
                downloader=market(small_settings, TUE), sleep=no_sleep)
    assert r.status == "failed" and r.exit_code == 1
    assert any("Resume" in m for m in n.sent)


def test_rerun_same_day_sends_skip_note(small_settings):
    db, n = InMemoryDB(10000), Quiet()
    dl = market(small_settings, TUE)
    execute(small_settings, db, n, now=at(TUE), downloader=dl, sleep=no_sleep)
    r = execute(small_settings, db, n, now=at(TUE), downloader=dl, sleep=no_sleep)
    assert r.status == "skipped"
    assert n.sent[-1].startswith("⏭️")
    assert db.runs[TUE].status == "success"


def test_failed_day_can_be_rerun(small_settings):
    db = InMemoryDB(10000)
    myt = ZoneInfo("Asia/Kuala_Lumpur")
    db.record_run(BotRun(TUE, "failed", "x", datetime.now(myt)))
    r = execute(small_settings, db, Quiet(), now=at(TUE),
                downloader=market(small_settings, TUE), sleep=no_sleep)
    assert r.status == "success" and db.runs[TUE].status == "success"


# ------------------------------------------------------------ 收市前不运行


def test_preclose_run_is_skipped_and_does_not_block_later(small_settings):
    db, n = InMemoryDB(10000), Quiet()
    myt = ZoneInfo("Asia/Kuala_Lumpur")
    morning = datetime(2026, 11, 24, 10, 37, tzinfo=myt)
    dl = market(small_settings, TUE)
    r = execute(small_settings, db, n, now=morning, downloader=dl, sleep=no_sleep)
    assert r.status == "skipped" and r.exit_code == 0
    assert db.get_snapshots() == [] and db.get_trades() == []
    assert n.sent[-1].startswith("⏰") and "10:37" in n.sent[-1]
    # 同一天收市后再跑，正常完成
    r2 = execute(small_settings, db, n, now=at(TUE), downloader=dl, sleep=no_sleep)
    assert r2.status == "success" and len(db.get_snapshots()) == 1


def test_cutoff_boundary(small_settings):
    myt = ZoneInfo("Asia/Kuala_Lumpur")
    dl = market(small_settings, TUE)
    r = execute(small_settings, InMemoryDB(10000), Quiet(), downloader=dl, sleep=no_sleep,
                now=datetime(2026, 11, 24, 17, 29, tzinfo=myt))
    assert r.status == "skipped"
    r = execute(small_settings, InMemoryDB(10000), Quiet(), downloader=dl, sleep=no_sleep,
                now=datetime(2026, 11, 24, 17, 30, tzinfo=myt))
    assert r.status == "success"


def test_latest_prices_saved_on_trading_day(small_settings):
    db = InMemoryDB(10000)
    execute(small_settings, db, Quiet(), now=at(TUE), downloader=market(small_settings, TUE),
            sleep=no_sleep)
    assert set(db.latest_prices) == set(small_settings.watchlist) | {small_settings.benchmark}
    assert db.latest_prices["1155.KL"] == (10.0, TUE)


def test_supabase_save_latest_prices_upserts():
    s = FakeSession([FakeResp(201, None)])
    SupabaseDB("https://abc.supabase.co", FAKE_KEY, market="MY", session=s).save_latest_prices(
        {"1155.KL": 10.14}, date(2026, 11, 24))
    method, url, kw = s.calls[0]
    assert url.endswith("/latest_prices") and kw["params"] == {"on_conflict": "market,ticker"}
    assert kw["json"][0]["market"] == "MY"
    assert kw["json"][0]["ticker"] == "1155.KL" and kw["json"][0]["price_date"] == "2026-11-24"


def test_supabase_reads_more_than_1000_rows_in_pages():
    """Supabase 每次最多回传 1000 行：超过的要分批读完（例如 4 年以上的每日快照）。"""
    snap = lambda i: {"snapshot_date": f"2020-01-01", "total_value": 1000 + i, "cash": 1000,
                      "positions_value": 0, "benchmark_close": 1500}
    s = FakeSession([FakeResp(200, [snap(i) for i in range(1000)]),
                     FakeResp(200, [snap(i) for i in range(1000, 1005)])])
    db = SupabaseDB("https://abc.supabase.co", FAKE_KEY, market="MY", session=s)
    snaps = db.get_snapshots()
    assert len(snaps) == 1005 and snaps[-1].total_value == 2004   # 最新的也拿到了
    offsets = [kw["params"]["offset"] for _, _, kw in s.calls]
    assert offsets == ["0", "1000"]
    assert all(kw["params"]["market"] == "eq.MY" and kw["params"]["limit"] == "1000"
               for _, _, kw in s.calls)


def test_supabase_single_page_makes_one_request():
    s = FakeSession([FakeResp(200, [])])
    SupabaseDB("https://abc.supabase.co", FAKE_KEY, market="US", session=s).get_trades()
    assert len(s.calls) == 1
