"""入口：一次完整运行的流程（PROJECT_SPEC 第 6 节）。

用法：
    python -m bot.main --market MY             # 正式运行（Supabase + Telegram）
    python -m bot.main --market US --dry-run   # 抓真实股价，但用内存数据库、只把讯息印出来

每个市场完全独立：自己的账户、时区、“今天”、运行记录；一个市场出错不影响另一个。

最高原则：只用虚拟钱；宁可不交易，也不要错误交易。
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
import time
import traceback
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Optional
from zoneinfo import ZoneInfo

from bot import data_fetcher as dfetch
from bot import safety
from bot.market import before_close_cutoff, local_now, local_today, price
from bot.broker import SimBroker
from bot.db import DatabaseError, DatabaseUnavailableError
from bot.config import (
    PROJECT_NAME,
    ROOT_DIR,
    ConfigError,
    MissingEnvError,
    Settings,
    available_markets,
    check_env,
    load_market,
)
from bot.models import BotRun, DailySnapshot, PendingOrder
from bot.notifier import (
    ConsoleNotifier,
    DailySummary,
    format_alert,
    format_holiday,
    format_preclose,
    format_rerun_skip,
    format_summary,
    format_trade,
)
from bot.risk import buy_cost, max_buy_quantity, stop_loss_triggered
from bot.strategy import history_needed, same_day_exit, strategy_signals

log = logging.getLogger(PROJECT_NAME)

LAST_RUN_PATH = ROOT_DIR / "logs" / "last_run.json"


class HaltTrading(Exception):
    """需要停止交易并警报的情况（带建议处理方式）。"""

    def __init__(self, error_type: str, detail: str, suggestion: str):
        self.error_type = error_type
        self.detail = detail
        self.suggestion = suggestion
        super().__init__(f"{error_type}: {detail}")


@dataclass
class RunOutcome:
    status: str               # success / skipped / failed
    message: str
    exit_code: int
    trades: list = field(default_factory=list)
    new_orders: list = field(default_factory=list)
    already_done: bool = False   # 今天已经成功过：不再写运行记录 / last_run.json


HOLIDAY_PREFIX = "今天休市"


# ====================================================================== 流程


def run_once(
    settings: Settings,
    db,
    notifier,
    now: datetime,
    downloader: dfetch.Downloader = dfetch.yfinance_downloader,
    sleep: Callable[[float], None] = time.sleep,
    quiet_repeat: bool = False,
    catchup: bool = False,
) -> RunOutcome:
    """跑一次完整流程。数据 / 风控问题抛 HaltTrading 或 DataError，由 execute() 统一处理。

    quiet_repeat：定时运行用。今天已经成功 → 安静跳过（不发 ⏭️）；收市前不发 ⏰；
    今天已经发过休市通知 → 不再重复发 📅。警报（⚠️）照常发。
    catchup：补跑过去错过的交易日。那天其实休市的话不发 📅（已经是过去的事）。
    """
    # 2. 用该市场自己的时区算“今天”（不能用服务器时间 / 马来西亚时间去算别的市场）
    today = local_today(settings, now)

    # 3. 幂等检查（该市场今天已 success → 跳过，只写 log；手动重跑才发简短通知）
    if safety.already_succeeded(db, today):
        msg = f"{settings.market} {today} 已经成功跑过，这次跳过（不会重复交易）"
        log.info(msg)
        if not quiet_repeat:
            notifier.send(format_rerun_skip(settings, today))
        return RunOutcome("skipped", msg, 0, already_done=True)

    # 收市前不运行：盘中价格不是收盘价，用来交易会出错（记录 skipped，不挡住之后的运行）
    if before_close_cutoff(settings, now):
        msg = (f"现在是{settings.timezone_label} {local_now(settings, now):%H:%M}，还没收市"
               f"（{settings.market_close_cutoff:%H:%M} 后才运行），这次不交易")
        if not quiet_repeat:
            notifier.send(format_preclose(settings, msg))
        return RunOutcome("skipped", msg, 0)

    # 状态一致性检查：本金和设定一致、现金不能是负数、持仓 / 挂单必须整手
    acct = db.ensure_account(settings.initial_cash, settings.currency)
    safety.check_capital(acct, settings.initial_cash, settings.market)
    safety.check_state(acct, db.get_positions(), db.get_pending_orders(), settings.lot_size)

    # 3. 抓数据（含重试）
    tickers = list(settings.watchlist) + [settings.benchmark]
    data = dfetch.fetch_all(
        tickers, settings.data.history_period, downloader,
        settings.data.retry_delays_seconds, sleep,
    )

    # 4. 数据验证（硬性错误会直接抛异常）
    report = dfetch.validate_all(
        data, settings.benchmark, today, history_needed(settings.strategy) - 1,
        settings.data.max_daily_move_pct, settings.data.max_stale_days,
    )
    for reason in list(report.suspicious.values()) + report.warnings:
        notifier.send(format_alert(
            "数据可疑", reason, "该股今天跳过（不交易、不执行挂单）；请到财经网站核对价格",
            settings.market,
        ))

    # 5. 交易日检查：最新数据的日期不是今天 → 休市
    latest = max(df.index[-1] for df in data.values())
    if latest != today:
        msg = f"{HOLIDAY_PREFIX}，没有新数据（最新数据日期 {latest}）"
        earlier = db.get_run(today) if quiet_repeat else None
        if catchup:
            log.info("补跑 %s：那天休市", today)
        elif earlier and earlier.message.startswith(HOLIDAY_PREFIX):
            log.info("今天已经发过休市通知，这次不重复发")
        else:
            notifier.send(format_holiday(settings))
        return RunOutcome("skipped", msg, 0)

    notes: list[str] = []
    bench_df = report.usable.get(settings.benchmark)
    if bench_df is None or bench_df.index[-1] != today:
        raise HaltTrading(
            "指数数据缺失", f"{settings.benchmark} 今天没有数据，无法记录每日快照",
            "确认 benchmark 代码；若 yfinance 只是延迟，稍后手动 Run workflow",
        )
    # 今天可交易的股票：通过验证、而且最新一天就是今天
    tradable = {
        t: df for t, df in report.usable.items()
        if t != settings.benchmark and df.index[-1] == today
    }
    for t in settings.watchlist:
        if t in report.usable and t not in tradable:
            notes.append(f"{t} 今天没有新数据（可能停牌），今天不交易这只股票")

    broker = SimBroker(db, settings)

    # 6. 执行挂单（昨天的信号，用今天开盘价）
    open_prices = {t: float(df["Open"].iloc[-1]) for t, df in tradable.items()}
    exec_res = broker.execute_pending(open_prices, today)
    for o, why in exec_res.cancelled:
        notes.append(f"取消挂单 {o.side} {o.ticker}：{why}")
    for o, qty, why in exec_res.reduced:
        notes.append(f"调整挂单 {o.side} {o.ticker}：{why}")
    for o in exec_res.deferred:
        notes.append(f"挂单 {o.side} {o.ticker} 今天没有可用数据，保留到下一个交易日")

    closes = {t: float(df["Close"].iloc[-1]) for t, df in tradable.items()}

    # 当天进出（rsi_daytrade）：今天开盘买进的，今天收盘就卖掉；盘中跌破止损价就按止损价卖
    if same_day_exit(settings.strategy):
        for t, pos in db.get_positions().items():
            if t not in tradable:
                notes.append(f"{t} 今天没有数据，无法当天平仓，保留到下一个交易日")
                continue
            day = tradable[t].iloc[-1]
            stop_price = round(pos.avg_price * (1 - settings.risk.stop_loss_pct), 4)
            if float(day["Low"]) <= stop_price:
                px, why = stop_price, f"当天止损：盘中跌破 {price(settings, stop_price)}"
            else:
                px, why = float(day["Close"]), "当天收盘平仓"
            exec_res.trades.append(broker.sell(t, pos.quantity, px, today, why))
    new_orders: list[PendingOrder] = []

    def pending_tickers() -> set[str]:
        return {o.ticker for o in db.get_pending_orders()}

    # 7. 止损检查 → 产生卖出挂单
    for t, pos in db.get_positions().items():
        if t not in closes or t in pending_tickers():
            continue
        if stop_loss_triggered(pos, closes[t], settings.risk.stop_loss_pct):
            reason = (
                f"止损：收盘 {price(settings, closes[t])} 跌破买入价 "
                f"{price(settings, pos.avg_price)} 的 {settings.risk.stop_loss_pct:.0%}"
            )
            new_orders.append(db.add_pending_order(PendingOrder(
                t, "SELL", pos.quantity, reason, today, closes[t])))

    # 8. 策略信号 → 风控 → 写入挂单
    strategy_data = {t: df for t, df in tradable.items() if t not in report.insufficient}
    signals = strategy_signals(settings.strategy, strategy_data, settings.watchlist,
                               db.get_positions(), today)
    for sig in signals:
        positions = db.get_positions()
        if sig.ticker in pending_tickers():
            continue
        if sig.side == "SELL":
            if sig.ticker in positions:
                new_orders.append(db.add_pending_order(PendingOrder(
                    sig.ticker, "SELL", positions[sig.ticker].quantity, sig.reason,
                    today, sig.close)))
            continue
        if sig.ticker in positions:
            continue  # 已持有，不加仓
        pending = db.get_pending_orders()
        pending_buys = [o for o in pending if o.side == "BUY"]
        reserved = sum(buy_cost(o.quantity, o.signal_price, settings.fees) for o in pending_buys)
        extra_new = len({o.ticker for o in pending_buys if o.ticker not in positions})
        cash = db.get_account().cash
        total = SimBroker.total_assets(cash, positions, closes)
        qty = max_buy_quantity(
            sig.ticker, sig.close, cash - reserved, total, positions,
            settings.risk, settings.fees, settings.lot_size,
            extra_new_positions=extra_new,
        )
        if qty <= 0:
            notes.append(f"{sig.ticker} 有买入信号（{sig.reason}），但风控不允许买入（现金或持仓数上限）")
            continue
        new_orders.append(db.add_pending_order(PendingOrder(
            sig.ticker, "BUY", qty, sig.reason, today, sig.close)))

    # 9. 每日快照（用今天收盘价估值；没有今天价格的持仓用平均成本）
    acct = db.get_account()
    positions = db.get_positions()
    pos_value = SimBroker.positions_value(positions, closes)
    total_value = round(acct.cash + pos_value, 2)
    bench_close = float(bench_df["Close"].iloc[-1])
    prev_snaps = [s for s in db.get_snapshots() if s.snapshot_date < today]
    db.save_snapshot(DailySnapshot(today, total_value, acct.cash, pos_value, bench_close))
    # 最新收盘价（dashboard 显示现价用；数据可疑 / 今天没数据的股票不更新）
    db.save_latest_prices({**closes, settings.benchmark: bench_close}, today)

    # 10. 记录成功（bot_runs 由 execute() 写入）
    # 11. 每日总结（就算没有交易也一定发）
    first_bench = prev_snaps[0].benchmark_close if prev_snaps else bench_close
    for o in new_orders:
        notes.append(f"新挂单（下一个交易日开盘执行）：{o.side} {o.quantity} × {o.ticker}｜{o.reason}")
    summary = DailySummary(
        run_date=str(today),
        total_value=total_value,
        day_change=round(total_value - prev_snaps[-1].total_value, 2) if prev_snaps else None,
        total_return=total_value / acct.initial_cash - 1,
        benchmark_return=bench_close / first_bench - 1,
        cash=acct.cash,
        positions_count=len(positions),
        trades=exec_res.trades,
        pending_count=len(db.get_pending_orders()),
        notes=notes,
    )
    for t in exec_res.trades:
        notifier.send(format_trade(settings, t))
    notifier.send(format_summary(settings, summary))
    return RunOutcome(
        "success", f"完成：成交 {len(exec_res.trades)} 笔，新挂单 {len(new_orders)} 笔", 0,
        trades=exec_res.trades, new_orders=new_orders,
    )


# ====================================================================== 外层防护


SUGGESTIONS = {
    dfetch.DataFetchError: "检查 config/markets.yaml 的 ticker 是否正确；" + dfetch.YFINANCE_HINT,
    dfetch.StaleDataError: dfetch.YFINANCE_HINT,
    dfetch.DataValidationError: "今天不交易，下一个交易日会自动再试；若连续出现请检查 yfinance 数据",
}


def short_traceback(exc: BaseException, limit: int = 3) -> str:
    tb = traceback.format_exception(type(exc), exc, exc.__traceback__, limit=-limit)
    text = "".join(tb).strip()
    return text[-1500:]


def execute(
    settings: Settings,
    db,
    notifier,
    now: Optional[datetime] = None,
    downloader: dfetch.Downloader = dfetch.yfinance_downloader,
    sleep: Callable[[float], None] = time.sleep,
    last_run_path: Optional[Path] = None,
    quiet_repeat: bool = False,
    catchup: bool = False,
    dedupe_alerts: Optional[bool] = None,
) -> RunOutcome:
    """最外层 try/except：任何异常 → 该市场不交易、发警报、记录 failed、非零退出码。

    dedupe_alerts（默认跟 quiet_repeat 一样）：同一天一模一样的失败已经记录过 → 不再发同样的提醒。
    """
    if dedupe_alerts is None:
        dedupe_alerts = quiet_repeat
    now = now or datetime.now(timezone.utc)
    started = datetime.now(timezone.utc)
    run_date = local_today(settings, now)
    mk = settings.market

    alerts: list[str] = []

    def alert(error_type: str, detail: str, suggestion: str) -> None:
        alerts.append(format_alert(error_type, detail, suggestion, mk))

    try:
        outcome = run_once(settings, db, notifier, now, downloader, sleep, quiet_repeat, catchup)
    except HaltTrading as e:
        alert(e.error_type, e.detail, e.suggestion)
        outcome = RunOutcome("failed", str(e), 1)
    except DatabaseUnavailableError as e:
        alert("数据库连不上，今天不交易", str(e),
              "到 Supabase Dashboard 打开项目，如果显示 Paused 就点 Resume，之后手动 Run workflow")
        outcome = RunOutcome("failed", f"{type(e).__name__}: {e}", 1)
    except DatabaseError as e:
        alert("数据库错误，今天不交易", str(e),
              "检查 SUPABASE_URL / SUPABASE_SERVICE_KEY 是否正确，以及 sql/ 里的 SQL 是否都已执行")
        outcome = RunOutcome("failed", f"{type(e).__name__}: {e}", 1)
    except safety.StateError as e:
        alert("账户状态不一致，今天不交易", str(e),
              f"到 Supabase Table Editor 检查 {mk} 的 accounts / positions / pending_orders，修正后再手动运行")
        outcome = RunOutcome("failed", f"{type(e).__name__}: {e}", 1)
    except dfetch.DataError as e:
        suggestion = next(
            (s for cls, s in SUGGESTIONS.items() if isinstance(e, cls)), dfetch.YFINANCE_HINT
        )
        alert(f"数据问题（{type(e).__name__}），今天不交易", str(e), suggestion)
        outcome = RunOutcome("failed", f"{type(e).__name__}: {e}", 1)
    except Exception as e:  # noqa: BLE001 - 最外层，必须全部接住
        alert(f"未预期的错误（{type(e).__name__}），今天不交易", short_traceback(e),
              "到 GitHub Actions 查看完整 log；修好前 bot 不会交易")
        outcome = RunOutcome("failed", f"{type(e).__name__}: {e}", 1)

    if alerts and dedupe_alerts and outcome.status == "failed" and _same_failure_already_recorded(db, run_date, outcome):
        log.warning("%s %s 同样的错误今天已经提醒过，这次不再发：%s", mk, run_date, outcome.message)
    else:
        for text in alerts:
            notifier.send(text)

    if outcome.already_done:   # 今天的成功记录和 last_run.json 保持原样
        return outcome

    try:
        db.record_run(BotRun(run_date, outcome.status, outcome.message[:1000],
                             started, datetime.now(timezone.utc)))
    except Exception as e:  # noqa: BLE001
        log.error("写入 bot_runs 失败：%s", e)
        notifier.send(format_alert("写入运行记录失败", str(e), "检查数据库连线", mk))
        outcome.exit_code = 1

    if last_run_path is not None and outcome.status != "failed":
        write_last_run(last_run_path, mk, run_date, outcome)
    return outcome


def _same_failure_already_recorded(db, run_date: date, outcome: RunOutcome) -> bool:
    """这一天已经记录过一模一样的失败（定时运行的后备 / 补跑重试）→ 不用再发一次提醒。

    查不到（例如数据库连不上）就当作没有，照样发提醒。
    """
    try:
        earlier = db.get_run(run_date)
    except Exception:  # noqa: BLE001
        return False
    return bool(earlier and earlier.status == "failed" and earlier.message == outcome.message[:1000])


# ====================================================================== 补跑错过的交易日

CATCHUP_LOOKBACK_DAYS = 7     # 最多往回补几天（日历天）


def close_time(settings: Settings, day: date) -> datetime:
    """某个交易日收市后的时间（当地 market_close_cutoff 再过 1 小时）。"""
    return datetime.combine(day, settings.market_close_cutoff, ZoneInfo(settings.timezone)) + timedelta(hours=1)


def missed_days(settings: Settings, db, now: datetime) -> list[date]:
    """上次成功之后、今天之前，还没成功处理的工作日（最旧的先）。

    GitHub 的定时运行可能晚好几个小时才启动（甚至过了午夜），这些日子要按顺序补上。
    从来没成功过（第一次运行）→ 不补。只往回看 CATCHUP_LOOKBACK_DAYS 天。
    """
    last = db.last_success_date()
    if last is None:
        return []
    today = local_today(settings, now)
    start = max(last + timedelta(days=1), today - timedelta(days=CATCHUP_LOOKBACK_DAYS))
    days, d = [], start
    while d < today:                     # 今天由正常流程处理（还没收市就只记录跳过）
        if d.weekday() < 5 and not db.has_successful_run(d):
            days.append(d)
        d += timedelta(days=1)
    return days


def execute_with_catchup(
    settings: Settings,
    db,
    notifier,
    now: Optional[datetime] = None,
    downloader: dfetch.Downloader = dfetch.yfinance_downloader,
    sleep: Callable[[float], None] = time.sleep,
    last_run_path: Optional[Path] = None,
    quiet_repeat: bool = False,
) -> RunOutcome:
    """正式运行用：先按顺序补跑错过的交易日（每天只用那天收市为止的数据），再处理今天。

    补跑的某一天失败 → 停下来（不跳着处理后面的日子），下次运行再试。
    """
    now = now or datetime.now(timezone.utc)
    cache: dict[str, object] = {}

    def cached(ticker: str, period: str):
        if ticker not in cache:
            cache[ticker] = downloader(ticker, period)
        return cache[ticker]

    try:
        days = missed_days(settings, db, now)
    except Exception as e:  # noqa: BLE001 - 查不到就当作没有要补的，正常流程会再碰到数据库问题
        log.warning("检查错过的交易日失败：%s", e)
        days = []

    for day in days:
        def upto(ticker: str, period: str, _day=day):
            df = dfetch.normalize(cached(ticker, period), ticker)
            return df.loc[:_day]

        log.info("补跑 %s %s", settings.market, day)
        out = execute(settings, db, notifier, now=close_time(settings, day), downloader=upto,
                      sleep=sleep, last_run_path=last_run_path, quiet_repeat=True, catchup=True,
                      dedupe_alerts=quiet_repeat)
        if out.status == "failed":
            return out

    return execute(settings, db, notifier, now=now, downloader=cached, sleep=sleep,
                   last_run_path=last_run_path, quiet_repeat=quiet_repeat)


def write_last_run(path: Path, market: str, run_date, outcome: RunOutcome) -> None:
    """logs/last_run.json：按市场分开记录，只更新本市场那一段，其他市场原样保留。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        existing = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        existing = {}
    markets = existing.get("markets") if isinstance(existing.get("markets"), dict) else {}
    markets[market] = {
        "run_date": str(run_date),
        "status": outcome.status,
        "message": outcome.message,
        "updated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    payload = {"project": PROJECT_NAME, "markets": dict(sorted(markets.items()))}
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


# ====================================================================== CLI


def main(argv: Optional[list[str]] = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except AttributeError:
            pass
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    parser = argparse.ArgumentParser(prog="python -m bot.main", description=f"{PROJECT_NAME} 模拟交易 bot")
    parser.add_argument("--market", required=True,
                        help="要运行的市场代码（config/markets.yaml 里的，例如 MY、US）")
    parser.add_argument("--dry-run", action="store_true",
                        help="抓真实股价，但不写数据库、不发讯息（用内存数据库）")
    parser.add_argument("--quiet-repeat", action="store_true",
                        help="定时后备运行：今天已经成功就安静跳过，休市通知每天只发一次")
    args = parser.parse_args(argv)
    market = args.market.strip().upper()

    safety.install_log_redaction()

    if not args.dry_run:
        # 本地从 .env 读；GitHub Actions 上已经有环境变量（Secrets），不会被 .env 覆盖
        from dotenv import load_dotenv

        load_dotenv(ROOT_DIR / ".env", override=False)

    console = ConsoleNotifier(prefix="[通知]" if not args.dry_run else "[DRY-RUN 通知]")
    try:
        settings = load_market(market)
    except ConfigError as e:
        try:
            known = ", ".join(available_markets())
        except ConfigError:
            known = "?"
        _emergency_alert(console, format_alert(
            "配置错误，今天不交易", str(e), f"修正 config/markets.yaml（可用市场：{known}）", market))
        return 1

    if args.dry_run:
        from bot.db import InMemoryDB

        print(f"=== {PROJECT_NAME} [{market}] DRY-RUN：模拟交易｜虚拟资金｜不写数据库、不发讯息 ===\n")
        outcome = execute(settings, InMemoryDB(settings.initial_cash, market), console)
        print(f"=== 结果：{outcome.status} — {outcome.message} ===")
        return outcome.exit_code

    # 1. 检查环境变量（只显示缺少的名称，不显示值）
    try:
        env = check_env()
    except MissingEnvError as e:
        _emergency_alert(console, format_alert(
            "缺少环境变量，今天不交易", str(e), "在 .env（本地）或 GitHub Secrets（云端）补上这些变量",
            market))
        return 1

    from bot.db import SupabaseDB
    from bot.notifier import TelegramNotifier

    print(f"=== {PROJECT_NAME} [{market}]：模拟交易｜虚拟资金 ===")
    notifier = TelegramNotifier(env["TELEGRAM_BOT_TOKEN"], env["TELEGRAM_CHAT_ID"])
    db = SupabaseDB(env["SUPABASE_URL"], env["SUPABASE_SERVICE_KEY"], market=market)
    outcome = execute_with_catchup(settings, db, notifier, last_run_path=LAST_RUN_PATH,
                                   quiet_repeat=args.quiet_repeat)
    print(f"=== 结果：{outcome.status} — {safety.redact(outcome.message)} ===")
    return outcome.exit_code


def _emergency_alert(console: ConsoleNotifier, text: str) -> None:
    """还没建好正式通知器时的警报：印出来；如果 Telegram 变量齐全也试着发出去。"""
    import os

    console.send(text)
    token, chat = os.environ.get("TELEGRAM_BOT_TOKEN"), os.environ.get("TELEGRAM_CHAT_ID")
    if token and chat:
        from bot.notifier import TelegramNotifier

        TelegramNotifier(token, chat).send(text)


if __name__ == "__main__":
    sys.exit(main())
