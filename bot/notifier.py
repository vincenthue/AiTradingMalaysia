"""通知讯息格式 + 发送。

第 1 阶段：ConsoleNotifier（只印在终端，--dry-run 用）。
第 2 阶段会加上 TelegramNotifier（重试 2 次，失败只写 log，不让程序崩溃）。

讯息里绝不能出现任何 token 或 key。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

from bot.config import PROJECT_NAME
from bot.models import Trade
from bot.safety import redact

log = logging.getLogger(__name__)


def format_trade(trade: Trade) -> str:
    icon = "🟢" if trade.side == "BUY" else "🔴"
    return (
        f"{icon} {trade.side} {trade.quantity} × {trade.ticker} @ RM {trade.price:.2f}"
        f"｜原因：{trade.reason}"
    )


@dataclass
class DailySummary:
    run_date: str
    total_value: float
    day_change: Optional[float]          # 金额，第一天为 None
    total_return: float                  # 比例
    benchmark_return: Optional[float]    # 比例，第一天为 0
    cash: float
    positions_count: int
    trades: list[Trade]
    pending_count: int
    notes: list[str]


def format_summary(s: DailySummary) -> str:
    lines = [f"📊 [{PROJECT_NAME}] {s.run_date} 每日总结（模拟交易｜虚拟资金）"]
    if s.trades:
        lines.append("今日交易：")
        lines += ["  " + format_trade(t) for t in s.trades]
    else:
        lines.append(f"今日无交易，总资产 RM {s.total_value:,.2f}")
    lines.append(f"总资产：RM {s.total_value:,.2f}")
    if s.day_change is None:
        lines.append("今日变化：—（第一天）")
    else:
        lines.append(f"今日变化：RM {s.day_change:+,.2f}")
    lines.append(f"总收益率：{s.total_return:+.2%}")
    bench = "—" if s.benchmark_return is None else f"{s.benchmark_return:+.2%}"
    lines.append(f"同期指数收益率：{bench}")
    lines.append(f"现金：RM {s.cash:,.2f}")
    lines.append(f"持仓数：{s.positions_count}")
    if s.pending_count:
        lines.append(f"明天开盘待执行挂单：{s.pending_count}")
    for n in s.notes:
        lines.append(f"ℹ️ {n}")
    return "\n".join(lines)


def format_alert(error_type: str, detail: str, suggestion: str) -> str:
    return f"⚠️ [{PROJECT_NAME}] {error_type}\n{detail}\n建议：{suggestion}"


def format_holiday() -> str:
    return f"📅 [{PROJECT_NAME}] 今天休市，没有新数据"


def format_rerun_skip(run_date) -> str:
    return f"⏭️ [{PROJECT_NAME}] {run_date} 今天已经跑过，这次跳过（不会重复交易）"


class ConsoleNotifier:
    """只把讯息印出来（dry-run / 本地用）。"""

    def __init__(self, prefix: str = "[DRY-RUN 通知]"):
        self.prefix = prefix
        self.sent: list[str] = []

    def send(self, text: str) -> bool:
        text = redact(text)
        self.sent.append(text)
        print(f"{self.prefix}\n{text}\n")
        return True


TELEGRAM_MAX_LEN = 4096


class TelegramNotifier:
    """发 Telegram 讯息。失败重试 2 次；还是失败就只写 log，绝不抛异常。"""

    def __init__(self, token: str, chat_id: str, session=None, retries: int = 2,
                 retry_delay: float = 3.0, sleep=None, timeout: float = 15.0):
        import time

        import requests

        self._url = f"https://api.telegram.org/bot{token}/sendMessage"
        self.chat_id = chat_id
        self.session = session or requests.Session()
        self.retries = retries
        self.retry_delay = retry_delay
        self.sleep = sleep or time.sleep
        self.timeout = timeout
        self.sent: list[str] = []

    def send(self, text: str) -> bool:
        text = redact(text)
        if len(text) > TELEGRAM_MAX_LEN:
            text = text[: TELEGRAM_MAX_LEN - 20] + "\n…（讯息太长已截断）"
        for attempt in range(1, self.retries + 2):
            try:
                resp = self.session.post(
                    self._url, json={"chat_id": self.chat_id, "text": text},
                    timeout=self.timeout,
                )
                if resp.status_code == 200:
                    self.sent.append(text)
                    return True
                # 只记录状态码和 Telegram 的说明，绝不记录网址（网址里有 token）
                try:
                    desc = resp.json().get("description", "")
                except Exception:  # noqa: BLE001
                    desc = ""
                log.warning("Telegram 发送失败（第 %d 次）：HTTP %s %s",
                            attempt, resp.status_code, redact(desc))
            except Exception as e:  # noqa: BLE001 - 发送失败不能让程序崩溃
                log.warning("Telegram 发送失败（第 %d 次）：%s", attempt, type(e).__name__)
            if attempt <= self.retries:
                self.sleep(self.retry_delay)
        log.error("Telegram 讯息最终没有发出（已重试 %d 次）。内容：\n%s", self.retries, text)
        return False
