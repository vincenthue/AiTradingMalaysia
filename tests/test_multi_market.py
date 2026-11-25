"""多市场：时区、每个市场自己的“今天”、幂等、市场隔离、讯息标签（全部用假数据）。"""
from dataclasses import replace
from datetime import date, datetime, time, timezone
from zoneinfo import ZoneInfo

import pytest

from bot import main as bot_main
from bot.db import InMemoryDB, InMemoryStore
from bot.main import execute
from bot.market import before_close_cutoff, local_today, money, price, tag
from bot.models import Trade
from bot.notifier import format_alert, format_holiday, format_trade
from tests.conftest import golden_cross_closes, make_df
from tests.test_main import Quiet, market, no_sleep

UTC = timezone.utc
NY = ZoneInfo("America/New_York")
MYT = ZoneInfo("Asia/Kuala_Lumpur")
MON, TUE, WED = date(2026, 11, 23), date(2026, 11, 24), date(2026, 11, 25)


@pytest.fixture
def us_settings(small_settings):
    """测试用的“美股”市场（第 3 步才会加入真正的 US 设定和费用）。"""
    return replace(
        small_settings, market="US", name="US Stocks", flag="🇺🇸", currency="USD",
        currency_symbol="$", timezone="America/New_York", timezone_label="纽约时间",
        market_close_cutoff=time(16, 30), initial_cash=200.0, benchmark="^GSPC", lot_size=1,
        watchlist=("BAC", "VZ", "T", "F"),
        risk=replace(small_settings.risk, max_position_pct=0.50),
    )


def us_close_run(d: date) -> datetime:
    """美股 workflow 的固定时间：UTC 22:30（纽约当天收市后；马来西亚隔天早上 6:30）。"""
    return datetime(d.year, d.month, d.day, 22, 30, tzinfo=UTC)


def my_close_run(d: date) -> datetime:
    return datetime(d.year, d.month, d.day, 18, 30, tzinfo=MYT)


# ------------------------------------------------------------ 时区


def test_us_run_in_malaysian_morning_is_previous_new_york_day(us_settings):
    """马来西亚 11/25 早上 6:30 跑美股 → 交易日是纽约的 11/24，不能算成 11/25。"""
    now = datetime(2026, 11, 25, 6, 30, tzinfo=MYT)
    assert local_today(us_settings, now) == TUE
    assert us_close_run(TUE) == now


def test_same_instant_different_today_per_market(small_settings, us_settings):
    now = us_close_run(TUE)                       # = MYT 11/25 06:30
    assert local_today(us_settings, now) == TUE
    assert local_today(small_settings, now) == WED


def test_us_cutoff_uses_new_york_time_in_summer_and_winter(us_settings):
    winter = datetime(2026, 1, 13, 22, 30, tzinfo=UTC)   # EST → 17:30 纽约
    summer = datetime(2026, 7, 14, 22, 30, tzinfo=UTC)   # EDT → 18:30 纽约
    assert not before_close_cutoff(us_settings, winter)
    assert not before_close_cutoff(us_settings, summer)
    open_hours = datetime(2026, 11, 24, 15, 0, tzinfo=NY)
    assert before_close_cutoff(us_settings, open_hours)


def test_naive_datetime_rejected(us_settings):
    with pytest.raises(ValueError):
        local_today(us_settings, datetime(2026, 11, 24, 22, 30))


def test_us_run_in_malaysian_morning_trades_new_york_day(us_settings):
    """美股数据最新是纽约 11/24 → 在马来西亚 11/25 早上跑，不能被当成休市。"""
    db, n = InMemoryDB(us_settings.initial_cash, "US"), Quiet()
    r = execute(us_settings, db, n, now=us_close_run(TUE),
                downloader=market(us_settings, TUE), sleep=no_sleep)
    assert r.status == "success"
    assert db.runs[TUE].status == "success" and WED not in db.runs


def test_us_holiday_detected_by_new_york_date(us_settings):
    """纽约 11/24 没有新数据（最新是 11/23）→ 美股休市。"""
    db, n = InMemoryDB(us_settings.initial_cash, "US"), Quiet()
    r = execute(us_settings, db, n, now=us_close_run(TUE),
                downloader=market(us_settings, MON), sleep=no_sleep)
    assert r.status == "skipped"
    assert any(m.startswith("📅 🇺🇸 [US]") for m in n.sent)


def test_us_preclose_manual_run_skipped(us_settings):
    db, n = InMemoryDB(us_settings.initial_cash, "US"), Quiet()
    now = datetime(2026, 11, 24, 11, 0, tzinfo=NY)  # 纽约盘中
    r = execute(us_settings, db, n, now=now, downloader=market(us_settings, TUE),
                sleep=no_sleep)
    assert r.status == "skipped" and "纽约时间 11:00" in r.message
    assert db.get_snapshots() == []


# ------------------------------------------------------------ 幂等（按市场）


def test_same_market_same_day_trades_once_but_other_market_still_runs(small_settings, us_settings):
    store = InMemoryStore()
    my = InMemoryDB(small_settings.initial_cash, "MY", store)
    us = InMemoryDB(us_settings.initial_cash, "US", store)
    gc = golden_cross_closes(n_flat=29)
    my_dl = market(small_settings, TUE, {"1155.KL": make_df(gc, TUE)})
    us_dl = market(us_settings, TUE, {"F": make_df([12.0 * x / 10 for x in gc], TUE)})

    assert execute(small_settings, my, Quiet(), now=my_close_run(TUE), downloader=my_dl,
                   sleep=no_sleep).status == "success"
    again = execute(small_settings, my, Quiet(), now=my_close_run(TUE), downloader=my_dl,
                    sleep=no_sleep)
    assert again.status == "skipped"
    assert len(my.get_pending_orders()) == 1               # MY 没有重复挂单

    # 同一个日期，US 照样可以跑（MY 的 success 不会挡住 US）
    r_us = execute(us_settings, us, Quiet(), now=us_close_run(TUE), downloader=us_dl,
                   sleep=no_sleep)
    assert r_us.status == "success"
    assert [o.ticker for o in us.get_pending_orders()] == ["F"]
    assert my.runs[TUE].status == "success" and us.runs[TUE].status == "success"


# ------------------------------------------------------------ 市场隔离


def test_my_failure_does_not_touch_us(small_settings, us_settings):
    store = InMemoryStore()
    my = InMemoryDB(small_settings.initial_cash, "MY", store)
    us = InMemoryDB(us_settings.initial_cash, "US", store)

    def broken(ticker, period):
        raise RuntimeError("yfinance 坏掉")

    n_my = Quiet()
    r_my = execute(small_settings, my, n_my, now=my_close_run(TUE), downloader=broken,
                   sleep=no_sleep)
    assert r_my.status == "failed" and r_my.exit_code == 1
    assert all("[MY]" in m for m in n_my.sent)            # 警报标明是 MY

    r_us = execute(us_settings, us, Quiet(), now=us_close_run(TUE),
                   downloader=market(us_settings, TUE), sleep=no_sleep)
    assert r_us.status == "success"
    assert us.get_account().cash == 200.0 and len(us.get_snapshots()) == 1
    assert my.get_snapshots() == [] and my.runs[TUE].status == "failed"


def test_trades_only_move_their_own_markets_cash(small_settings, us_settings):
    store = InMemoryStore()
    my = InMemoryDB(1000.0, "MY", store)
    us = InMemoryDB(200.0, "US", store)
    us.execute_buy("F", 5, 12.0, 1.0, TUE, "test")
    assert us.get_account().cash == 139.0
    assert my.get_account().cash == 1000.0
    assert "F" not in my.get_positions() and my.get_trades() == []
    # US 账户不能卖 MY 的股票（就算 MY 持有）
    my.execute_buy("1155.KL", 100, 1.0, 8.0, TUE, "test")
    with pytest.raises(Exception):
        us.execute_sell("1155.KL", 100, 1.0, 1.0, TUE, "test")
    assert my.get_positions()["1155.KL"].quantity == 100


def test_failed_transaction_rolls_back_only_that_market(small_settings):
    store = InMemoryStore()
    my = InMemoryDB(1000.0, "MY", store)
    us = InMemoryDB(200.0, "US", store)
    us.execute_buy("F", 1, 12.0, 1.0, TUE, "test")
    with pytest.raises(Exception):
        my.execute_buy("X.KL", 100, 1.0, 8.0, TUE, "test", order_id=999)  # 挂单不存在 → 回滚
    assert my.get_account().cash == 1000.0 and my.get_trades() == []
    assert us.get_account().cash == 187.0 and len(us.get_trades()) == 1


def test_capital_mismatch_is_per_market(us_settings):
    db = InMemoryDB(2500.0, "US")      # 数据库还是旧本金
    n = Quiet()
    r = execute(us_settings, db, n, now=us_close_run(TUE),
                downloader=market(us_settings, TUE), sleep=no_sleep)
    assert r.status == "failed"
    assert any("[US]" in m and "initial_cash" in m for m in n.sent)


# ------------------------------------------------------------ 讯息格式


def test_money_and_price_use_market_currency(small_settings, us_settings):
    assert money(small_settings, 1234.5) == "RM 1,234.50"
    assert money(us_settings, 1234.5) == "$1,234.50"
    assert money(us_settings, -3.2, signed=True) == "-$3.20"
    assert money(small_settings, 5, signed=True) == "+RM 5.00"
    assert price(small_settings, 0.845) == "RM 0.845"
    assert price(us_settings, 142.3) == "$142.30"


def test_messages_are_tagged_with_market(small_settings, us_settings):
    t = Trade(TUE, "NVDA", "SELL", 5, 142.30, 1.0, "止损 -8%")
    assert format_trade(us_settings, t) == "🇺🇸 [US] 🔴 SELL 5 × NVDA @ $142.30｜原因：止损 -8%"
    t2 = Trade(TUE, "5347.KL", "BUY", 300, 13.20, 9.0, "50MA 上穿 200MA")
    assert format_trade(small_settings, t2) == (
        "🇲🇾 [MY] 🟢 BUY 300 × 5347.KL @ RM 13.20｜原因：50MA 上穿 200MA")
    assert format_holiday(small_settings) == "📅 🇲🇾 [MY] 今天休市，没有新数据"
    assert format_alert("数据问题", "x", "y", "US").startswith("⚠️ [AiTradingMalaysia][US] 数据问题")
    assert tag(us_settings) == "🇺🇸 [US]"


def test_summary_in_market_currency(us_settings):
    db, n = InMemoryDB(us_settings.initial_cash, "US"), Quiet()
    execute(us_settings, db, n, now=us_close_run(TUE), downloader=market(us_settings, TUE),
            sleep=no_sleep)
    summary = n.sent[-1]
    assert summary.startswith("📊 🇺🇸 [US] 2026-11-24 每日总结")
    assert "总资产：$200.00" in summary and "RM" not in summary


# ------------------------------------------------------------ CLI


def test_cli_requires_market():
    with pytest.raises(SystemExit):
        bot_main.main(["--dry-run"])


def test_cli_unknown_market_alerts_and_exits(capsys):
    assert bot_main.main(["--market", "XX", "--dry-run"]) == 1
    out = capsys.readouterr().out
    assert "[XX]" in out and "MY" in out


def test_every_us_message_is_tagged_and_has_no_ringgit(us_settings):
    """美股所有讯息：开头有 🇺🇸 [US]（警报是 [AiTradingMalaysia][US]），不能出现 RM。"""
    from bot.notifier import DailySummary, format_preclose, format_rerun_skip, format_summary
    t = Trade(TUE, "F", "BUY", 8, 11.95, 1.0, "50MA 上穿 200MA")
    s = DailySummary("2026-11-24", 199.0, -1.0, -0.005, 0.01, 103.4, 1, [t], 0, ["x"])
    msgs = {
        "trade": format_trade(us_settings, t),
        "summary": format_summary(us_settings, s),
        "holiday": format_holiday(us_settings),
        "preclose": format_preclose(us_settings, "现在是纽约时间 11:00"),
        "rerun": format_rerun_skip(us_settings, TUE),
    }
    for name, m in msgs.items():
        assert "🇺🇸 [US]" in m.splitlines()[0], name
        assert "RM" not in m, name
    assert "同期指数收益率（^GSPC）" in msgs["summary"]
    assert "今日变化：-$1.00" in msgs["summary"]
    assert format_alert("x", "y", "z", "US").splitlines()[0] == "⚠️ [AiTradingMalaysia][US] x"


def test_negative_cash_alert_has_no_currency_symbol(us_settings):
    from bot.models import Account
    from bot.safety import StateError, check_state
    with pytest.raises(StateError) as ei:
        check_state(Account(-5, 200), {}, [], 1)
    assert "RM" not in str(ei.value)


# ------------------------------------------------------------ 定时后备运行（--quiet-repeat）


def test_backup_run_after_success_is_silent_and_keeps_records(small_settings, tmp_path):
    import json
    db, n = InMemoryDB(small_settings.initial_cash, "MY"), Quiet()
    p = tmp_path / "last_run.json"
    dl = market(small_settings, TUE)
    execute(small_settings, db, n, now=my_close_run(TUE), downloader=dl, sleep=no_sleep, last_run_path=p)
    first = p.read_text(encoding="utf-8")
    sent_before = len(n.sent)
    r = execute(small_settings, db, n, now=my_close_run(TUE), downloader=dl, sleep=no_sleep,
                last_run_path=p, quiet_repeat=True)
    assert r.status == "skipped" and r.already_done and r.exit_code == 0
    assert len(n.sent) == sent_before                      # 不发 ⏭️
    assert db.runs[TUE].status == "success"                # 成功记录不动
    assert p.read_text(encoding="utf-8") == first          # last_run.json 不被改成 skipped
    assert json.loads(first)["markets"]["MY"]["status"] == "success"


def test_manual_rerun_still_sends_skip_note(small_settings):
    db, n = InMemoryDB(small_settings.initial_cash, "MY"), Quiet()
    dl = market(small_settings, TUE)
    execute(small_settings, db, n, now=my_close_run(TUE), downloader=dl, sleep=no_sleep)
    execute(small_settings, db, n, now=my_close_run(TUE), downloader=dl, sleep=no_sleep)
    assert n.sent[-1].startswith("⏭️ 🇲🇾 [MY]")


def test_backup_run_retries_when_data_was_late(small_settings):
    """18:37 时 Yahoo 还没有今天的数据（看起来像休市）→ 后备运行拿到数据就正常完成。"""
    db, n = InMemoryDB(small_settings.initial_cash, "MY"), Quiet()
    r1 = execute(small_settings, db, n, now=my_close_run(TUE), downloader=market(small_settings, MON),
                 sleep=no_sleep)
    assert r1.status == "skipped" and n.sent[-1].startswith("📅")
    r2 = execute(small_settings, db, n, now=my_close_run(TUE), downloader=market(small_settings, TUE),
                 sleep=no_sleep, quiet_repeat=True)
    assert r2.status == "success" and n.sent[-1].startswith("📊 🇲🇾 [MY]")
    assert db.runs[TUE].status == "success" and len(db.get_snapshots()) == 1


def test_backup_run_retries_after_failure(small_settings):
    db, n = InMemoryDB(small_settings.initial_cash, "MY"), Quiet()

    def broken(ticker, period):
        raise RuntimeError("Yahoo 暂时坏掉")

    r1 = execute(small_settings, db, n, now=my_close_run(TUE), downloader=broken, sleep=no_sleep)
    assert r1.status == "failed"
    r2 = execute(small_settings, db, n, now=my_close_run(TUE), downloader=market(small_settings, TUE),
                 sleep=no_sleep, quiet_repeat=True)
    assert r2.status == "success" and db.runs[TUE].status == "success"


def test_holiday_notice_sent_once_per_day(small_settings):
    db, n = InMemoryDB(small_settings.initial_cash, "MY"), Quiet()
    holiday = market(small_settings, MON)                  # 今天（TUE）没有新数据
    for quiet in (False, True, True):
        execute(small_settings, db, n, now=my_close_run(TUE), downloader=holiday, sleep=no_sleep,
                quiet_repeat=quiet)
    assert sum(m.startswith("📅") for m in n.sent) == 1
    assert db.runs[TUE].status == "skipped"


def test_supabase_get_run():
    from tests.test_phase2 import FAKE_KEY, FakeResp, FakeSession
    from bot.db import SupabaseDB
    row = {"run_date": "2026-11-24", "status": "skipped", "message": "今天休市，没有新数据",
           "started_at": "2026-11-24T10:37:00+00:00", "finished_at": None}
    s = FakeSession([FakeResp(200, [row]), FakeResp(200, [])])
    db = SupabaseDB("https://abc.supabase.co", FAKE_KEY, market="MY", session=s)
    assert db.get_run(TUE).message.startswith("今天休市")
    assert db.get_run(WED) is None
    assert s.calls[0][2]["params"]["market"] == "eq.MY"
