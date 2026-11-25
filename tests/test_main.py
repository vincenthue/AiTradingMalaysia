"""完整流程测试（假数据、内存数据库、假通知）。"""
from datetime import date, datetime
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
import pytest

from bot import main as bot_main
from bot.db import InMemoryDB
from bot.main import execute
from bot.models import Position
from bot.notifier import ConsoleNotifier
from tests.conftest import golden_cross_closes, make_df

MYT = ZoneInfo("Asia/Kuala_Lumpur")
MON = date(2026, 11, 23)
TUE = date(2026, 11, 24)
WED = date(2026, 11, 25)


def at(d: date) -> datetime:
    return datetime(d.year, d.month, d.day, 18, 30, tzinfo=MYT)


class Quiet(ConsoleNotifier):
    def send(self, text):
        self.sent.append(text)
        return True


def market(settings, end: date, special=None):
    """全部股票平稳，special 可以覆盖某些股票的数据。"""
    data = {t: make_df([10.0] * 30, end) for t in settings.watchlist}
    data[settings.benchmark] = make_df([1500.0] * 30, end)
    data.update(special or {})
    return lambda ticker, period: data[ticker]


def no_sleep(_):
    pass


@pytest.fixture
def env(small_settings):
    return small_settings, InMemoryDB(small_settings.initial_cash), Quiet()


def run(env, when, downloader):
    st, db, n = env
    return execute(st, db, n, now=at(when), downloader=downloader, sleep=no_sleep)


# ------------------------------------------------------------ 正常流程


def test_signal_today_fills_next_day_at_open(env):
    st, db, n = env
    gc = golden_cross_closes(n_flat=29)
    r1 = run(env, TUE, market(st, TUE, {"1155.KL": make_df(gc, TUE)}))
    assert r1.status == "success" and r1.trades == []
    orders = db.get_pending_orders()
    assert len(orders) == 1 and orders[0].side == "BUY" and orders[0].ticker == "1155.KL"
    assert orders[0].quantity % 100 == 0
    assert any("今日无交易" in m for m in n.sent)  # 没有交易也一定发总结

    # 第二天：用开盘价（不是收盘价）成交
    nxt = make_df(gc + [gc[-1]], WED)
    nxt.loc[WED, "Open"] = 12.0
    r2 = run(env, WED, market(st, WED, {"1155.KL": nxt}))
    assert r2.status == "success"
    assert len(r2.trades) == 1 and r2.trades[0].price == 12.0
    assert db.get_pending_orders() == []
    assert db.get_positions()["1155.KL"].quantity == r2.trades[0].quantity
    assert len(db.get_snapshots()) == 2


def test_idempotent_same_day_twice(env):
    st, db, n = env
    gc = golden_cross_closes(n_flat=29)
    dl = market(st, TUE, {"1155.KL": make_df(gc, TUE)})
    run(env, TUE, dl)
    r2 = run(env, TUE, dl)
    assert r2.status == "skipped"
    assert len(db.get_pending_orders()) == 1   # 没有重复挂单
    assert db.runs[TUE].status == "success"    # 成功记录没被覆盖


def test_holiday_detected(env):
    st, db, n = env
    r = run(env, TUE, market(st, MON))  # 最新数据是昨天
    assert r.status == "skipped" and r.exit_code == 0
    assert any("休市" in m for m in n.sent)
    assert db.get_trades() == [] and db.get_snapshots() == []


def test_stop_loss_creates_sell_order(env):
    st, db, n = env
    db.upsert_position(Position("5347.KL", 100, 10.0, MON))
    run(env, TUE, market(st, TUE, {"5347.KL": make_df([10.0] * 29 + [9.0], TUE)}))
    orders = db.get_pending_orders()
    assert [(o.ticker, o.side, o.quantity) for o in orders] == [("5347.KL", "SELL", 100)]
    assert "止损" in orders[0].reason


def test_suspicious_stock_skipped_and_alerted(env):
    st, db, n = env
    from bot.models import PendingOrder
    db.add_pending_order(PendingOrder("1155.KL", "BUY", 100, "金叉", MON, 10.0))
    r = run(env, TUE, market(st, TUE, {"1155.KL": make_df([10.0] * 29 + [14.0], TUE)}))
    assert r.status == "success"
    assert r.trades == [] and len(db.get_pending_orders()) == 1  # 挂单保留，不执行
    assert any("数据可疑" in m for m in n.sent)


# ------------------------------------------------------------ 异常 → 警报、不交易


def _assert_failed_no_trade(r, db, n):
    assert r.status == "failed" and r.exit_code != 0
    assert db.get_trades() == []
    assert db.runs[TUE].status == "failed"
    assert any(m.startswith("⚠️ [AiTradingMalaysia]") for m in n.sent)


def test_fetch_failure_alerts_and_no_trade(env):
    st, db, n = env
    from bot.models import PendingOrder
    db.add_pending_order(PendingOrder("1155.KL", "BUY", 100, "金叉", MON, 10.0))

    def dl(ticker, period):
        if ticker == "1023.KL":
            raise ConnectionError("boom")
        return market(st, TUE)(ticker, period)

    r = run(env, TUE, dl)
    _assert_failed_no_trade(r, db, n)
    assert len(db.get_pending_orders()) == 1
    assert any("yfinance 版本" in m for m in n.sent)


def test_bad_ticker_alerts(env):
    st, db, n = env
    st2 = st.__class__(**{**st.__dict__, "watchlist": st.watchlist + ("9999X.KL",)})
    base = market(st, TUE)
    r = execute(st2, db, n, now=at(TUE), sleep=no_sleep,
                downloader=lambda t, p: pd.DataFrame() if t == "9999X.KL" else base(t, p))
    _assert_failed_no_trade(r, db, n)
    assert any("9999X.KL" in m for m in n.sent)


def test_nan_price_alerts_and_no_trade(env):
    st, db, n = env
    bad = make_df([10.0] * 30, TUE)
    bad.loc[TUE, "Close"] = np.nan
    r = run(env, TUE, market(st, TUE, {"1295.KL": bad}))
    _assert_failed_no_trade(r, db, n)


def test_stale_data_alerts_and_no_trade(env):
    st, db, n = env
    r = run(env, TUE, market(st, date(2026, 11, 16)))
    _assert_failed_no_trade(r, db, n)


def test_unexpected_exception_alerts_and_fails(env, monkeypatch):
    """未预期的异常 → 最外层接住，发警报（含错误类型），记录 failed。"""
    st, db, n = env

    def boom(*a, **k):
        raise KeyError("unexpected")

    monkeypatch.setattr(bot_main, "generate_signals", boom)
    r = run(env, TUE, market(st, TUE))
    _assert_failed_no_trade(r, db, n)
    assert any("KeyError" in m for m in n.sent)


def test_notifier_failure_does_not_crash(env):
    st, db, _ = env

    class Broken:
        def send(self, text):
            return False  # Telegram 失败：只回传 False，不抛异常

    r = execute(st, db, Broken(), now=at(TUE), downloader=market(st, TUE), sleep=no_sleep)
    assert r.status == "success"


def test_last_run_json_written(env, tmp_path):
    st, db, n = env
    p = tmp_path / "logs" / "last_run.json"
    execute(st, db, n, now=at(TUE), downloader=market(st, TUE), sleep=no_sleep, last_run_path=p)
    import json
    data = json.loads(p.read_text(encoding="utf-8"))
    my = data["markets"]["MY"]
    assert my["run_date"] == "2026-11-24" and my["status"] == "success"


def test_last_run_json_keeps_other_markets(env, tmp_path):
    """两个市场共用 last_run.json：写 MY 不会覆盖 US 那一段。"""
    import json
    st, db, n = env
    p = tmp_path / "last_run.json"
    p.write_text(json.dumps({"project": "x", "markets": {"US": {"status": "success"}}}),
                 encoding="utf-8")
    execute(st, db, n, now=at(TUE), downloader=market(st, TUE), sleep=no_sleep, last_run_path=p)
    data = json.loads(p.read_text(encoding="utf-8"))
    assert data["markets"]["US"] == {"status": "success"}
    assert data["markets"]["MY"]["status"] == "success"


def test_cli_non_dry_run_lists_missing_env_names_only(monkeypatch, capsys):
    for k in ("SUPABASE_URL", "SUPABASE_SERVICE_KEY", "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123:SECRET")
    code = bot_main.main(["--market", "MY"])
    out = capsys.readouterr().out
    assert code == 1
    assert "SUPABASE_URL" in out and "SECRET" not in out
