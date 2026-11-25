"""入口：一次完整运行的流程（PROJECT_SPEC 第 6 节）。

用法：
    python -m bot.main --dry-run     # 抓真实股价，但用内存数据库、只把讯息印出来

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
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional
from zoneinfo import ZoneInfo

from bot import data_fetcher as dfetch
from bot import safety
from bot.broker import SimBroker
from bot.db import DatabaseError, DatabaseUnavailableError
from bot.config import (
    PROJECT_NAME,
    ROOT_DIR,
    ConfigError,
    MissingEnvError,
    Settings,
    check_env,
    load_settings,
)
from bot.models import BotRun, DailySnapshot, PendingOrder
from bot.notifier import (
    ConsoleNotifier,
    DailySummary,
    format_alert,
    format_holiday,
    format_rerun_skip,
    format_summary,
    format_trade,
)
from bot.risk import buy_cost, max_buy_quantity, stop_loss_triggered
from bot.strategy import generate_signals

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


# ====================================================================== 流程


def run_once(
    settings: Settings,
    db,
    notifier,
    now: datetime,
    downloader: dfetch.Downloader = dfetch.yfinance_downloader,
    sleep: Callable[[float], None] = time.sleep,
) -> RunOutcome:
    """跑一次完整流程。数据 / 风控问题抛 HaltTrading 或 DataError，由 execute() 统一处理。"""
    tz = ZoneInfo(settings.timezone)
    today = now.astimezone(tz).date()

    # 2. 幂等检查（已 success 的记录不会被覆盖，跳过只写 log + 发简短通知）
    if safety.already_succeeded(db, today):
        msg = f"{today} 已经成功跑过，这次跳过（不会重复交易）"
        log.info(msg)
        notifier.send(format_rerun_skip(today))
        return RunOutcome("skipped", msg, 0)

    # 状态一致性检查：现金不能是负数、持仓 / 挂单必须整手
    acct = db.ensure_account(settings.initial_cash)
    safety.check_state(acct, db.get_positions(), db.get_pending_orders(), settings.lot_size)

    # 3. 抓数据（含重试）
    tickers = list(settings.watchlist) + [settings.benchmark]
    data = dfetch.fetch_all(
        tickers, settings.data.history_period, downloader,
        settings.data.retry_delays_seconds, sleep,
    )

    # 4. 数据验证（硬性错误会直接抛异常）
    report = dfetch.validate_all(
        data, settings.benchmark, today, settings.strategy.long_window,
        settings.data.max_daily_move_pct, settings.data.max_stale_days,
    )
    for reason in list(report.suspicious.values()) + report.warnings:
        notifier.send(format_alert(
            "数据可疑", reason, "该股今天跳过（不交易、不执行挂单）；请到财经网站核对价格"
        ))

    # 5. 交易日检查：最新数据的日期不是今天 → 休市
    latest = max(df.index[-1] for df in data.values())
    if latest != today:
        msg = f"今天休市，没有新数据（最新数据日期 {latest}）"
        notifier.send(format_holiday())
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
    new_orders: list[PendingOrder] = []

    def pending_tickers() -> set[str]:
        return {o.ticker for o in db.get_pending_orders()}

    # 7. 止损检查 → 产生卖出挂单
    for t, pos in db.get_positions().items():
        if t not in closes or t in pending_tickers():
            continue
        if stop_loss_triggered(pos, closes[t], settings.risk.stop_loss_pct):
            reason = (
                f"止损：收盘 RM {closes[t]:.2f} 跌破买入价 RM {pos.avg_price:.2f} 的 "
                f"{settings.risk.stop_loss_pct:.0%}"
            )
            new_orders.append(db.add_pending_order(PendingOrder(
                t, "SELL", pos.quantity, reason, today, closes[t])))

    # 8. 策略信号 → 风控 → 写入挂单
    strategy_data = {t: df for t, df in tradable.items() if t not in report.insufficient}
    signals = generate_signals(
        strategy_data, settings.watchlist,
        settings.strategy.short_window, settings.strategy.long_window,
    )
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

    # 10. 记录成功（bot_runs 由 execute() 写入）
    # 11. 每日总结（就算没有交易也一定发）
    first_bench = prev_snaps[0].benchmark_close if prev_snaps else bench_close
    for o in new_orders:
        notes.append(f"新挂单（明天开盘执行）：{o.side} {o.quantity} × {o.ticker}｜{o.reason}")
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
        notifier.send(format_trade(t))
    notifier.send(format_summary(summary))
    return RunOutcome(
        "success", f"完成：成交 {len(exec_res.trades)} 笔，新挂单 {len(new_orders)} 笔", 0,
        trades=exec_res.trades, new_orders=new_orders,
    )


# ====================================================================== 外层防护


SUGGESTIONS = {
    dfetch.DataFetchError: "检查 settings.yaml 的 ticker 是否正确；" + dfetch.YFINANCE_HINT,
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
) -> RunOutcome:
    """最外层 try/except：任何异常 → 不交易、发警报、记录 failed、非零退出码。"""
    tz = ZoneInfo(settings.timezone)
    now = now or datetime.now(tz)
    started = datetime.now(tz)
    run_date = now.astimezone(tz).date()
    try:
        outcome = run_once(settings, db, notifier, now, downloader, sleep)
    except HaltTrading as e:
        notifier.send(format_alert(e.error_type, e.detail, e.suggestion))
        outcome = RunOutcome("failed", str(e), 1)
    except DatabaseUnavailableError as e:
        notifier.send(format_alert("数据库连不上，今天不交易", str(e),
                                   "到 Supabase Dashboard 打开项目，如果显示 Paused 就点 Resume，之后手动 Run workflow"))
        outcome = RunOutcome("failed", f"{type(e).__name__}: {e}", 1)
    except DatabaseError as e:
        notifier.send(format_alert("数据库错误，今天不交易", str(e),
                                   "检查 SUPABASE_URL / SUPABASE_SERVICE_KEY 是否正确，以及 schema.sql 是否已执行"))
        outcome = RunOutcome("failed", f"{type(e).__name__}: {e}", 1)
    except safety.StateError as e:
        notifier.send(format_alert("账户状态不一致，今天不交易", str(e),
                                   "到 Supabase Table Editor 检查 account / positions / pending_orders，修正后再手动运行"))
        outcome = RunOutcome("failed", f"{type(e).__name__}: {e}", 1)
    except dfetch.DataError as e:
        suggestion = next(
            (s for cls, s in SUGGESTIONS.items() if isinstance(e, cls)), dfetch.YFINANCE_HINT
        )
        notifier.send(format_alert(f"数据问题（{type(e).__name__}），今天不交易", str(e), suggestion))
        outcome = RunOutcome("failed", f"{type(e).__name__}: {e}", 1)
    except Exception as e:  # noqa: BLE001 - 最外层，必须全部接住
        notifier.send(format_alert(
            f"未预期的错误（{type(e).__name__}），今天不交易",
            short_traceback(e),
            "到 GitHub Actions 查看完整 log；修好前 bot 不会交易",
        ))
        outcome = RunOutcome("failed", f"{type(e).__name__}: {e}", 1)

    try:
        db.record_run(BotRun(run_date, outcome.status, outcome.message[:1000],
                             started, datetime.now(tz)))
    except Exception as e:  # noqa: BLE001
        log.error("写入 bot_runs 失败：%s", e)
        notifier.send(format_alert("写入运行记录失败", str(e), "检查数据库连线"))
        outcome.exit_code = 1

    if last_run_path is not None and outcome.status != "failed":
        write_last_run(last_run_path, run_date, outcome)
    return outcome


def write_last_run(path: Path, run_date, outcome: RunOutcome) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "project": PROJECT_NAME,
        "run_date": str(run_date),
        "status": outcome.status,
        "message": outcome.message,
        "updated_at": datetime.now(ZoneInfo("UTC")).isoformat(timespec="seconds"),
    }
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
    parser.add_argument("--dry-run", action="store_true",
                        help="抓真实股价，但不写数据库、不发讯息（用内存数据库）")
    args = parser.parse_args(argv)

    safety.install_log_redaction()

    if not args.dry_run:
        # 本地从 .env 读；GitHub Actions 上已经有环境变量（Secrets），不会被 .env 覆盖
        from dotenv import load_dotenv

        load_dotenv(ROOT_DIR / ".env", override=False)

    console = ConsoleNotifier(prefix="[通知]" if not args.dry_run else "[DRY-RUN 通知]")
    try:
        settings = load_settings()
    except ConfigError as e:
        _emergency_alert(console, format_alert("配置错误", str(e), "修正 config/settings.yaml"))
        return 1

    if args.dry_run:
        from bot.db import InMemoryDB

        print(f"=== {PROJECT_NAME} DRY-RUN：模拟交易｜虚拟资金｜不写数据库、不发讯息 ===\n")
        outcome = execute(settings, InMemoryDB(settings.initial_cash), console)
        print(f"=== 结果：{outcome.status} — {outcome.message} ===")
        return outcome.exit_code

    # 1. 检查环境变量（只显示缺少的名称，不显示值）
    try:
        env = check_env()
    except MissingEnvError as e:
        _emergency_alert(console, format_alert(
            "缺少环境变量，今天不交易", str(e), "在 .env（本地）或 GitHub Secrets（云端）补上这些变量"))
        return 1

    from bot.db import SupabaseDB
    from bot.notifier import TelegramNotifier

    print(f"=== {PROJECT_NAME}：模拟交易｜虚拟资金 ===")
    notifier = TelegramNotifier(env["TELEGRAM_BOT_TOKEN"], env["TELEGRAM_CHAT_ID"])
    db = SupabaseDB(env["SUPABASE_URL"], env["SUPABASE_SERVICE_KEY"])
    outcome = execute(settings, db, notifier, last_run_path=LAST_RUN_PATH)
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
