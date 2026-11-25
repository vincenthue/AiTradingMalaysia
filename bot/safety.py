"""安全防护：幂等检查、状态一致性检查、密钥遮蔽。

- 幂等：同一天（马来西亚时间）已经 success → 这次跳过，不会重复交易。
  （并发锁由 GitHub Actions 的 concurrency 设定负责，见第 3 阶段。）
- 状态一致性：现金不能是负数；持仓 / 挂单数量必须是 lot_size 的整数倍。
- 密钥遮蔽：任何要写进 log 或发出去的文字，都先把密钥换成 ***。
"""
from __future__ import annotations

import logging
import os
import re
from datetime import date
from typing import Iterable

from bot.config import REQUIRED_ENV_VARS
from bot.models import Account, PendingOrder, Position


class StateError(Exception):
    """账户状态不一致，停止交易。"""


def check_capital(account: Account, configured: float, market: str = "") -> None:
    """markets.yaml 的本金必须和数据库账户一致（防止改了设定档却忘了改数据库）。"""
    if abs(account.initial_cash - configured) > 0.005:
        raise StateError(
            f"markets.yaml 的 {market} initial_cash 是 {configured:,.2f}，"
            f"但数据库 {market} 账户的 initial_cash 是 {account.initial_cash:,.2f}。"
            "请把两边改成一致（数据库：Supabase SQL Editor 更新 accounts 表）"
        )


def already_succeeded(db, run_date: date) -> bool:
    return db.has_successful_run(run_date)


def check_state(
    account: Account,
    positions: dict[str, Position],
    pending: Iterable[PendingOrder],
    lot_size: int,
) -> None:
    problems: list[str] = []
    if account.cash < 0:
        problems.append(f"现金是负数（{account.cash:,.2f}）")
    if account.initial_cash <= 0:
        problems.append("初始资金 <= 0")
    for t, p in positions.items():
        if p.quantity <= 0 or p.quantity % lot_size != 0:
            problems.append(f"{t} 持仓 {p.quantity} 股不是 {lot_size} 的整数倍")
        if p.avg_price <= 0:
            problems.append(f"{t} 平均买入价无效（{p.avg_price}）")
    for o in pending:
        if o.quantity <= 0 or o.quantity % lot_size != 0:
            problems.append(f"挂单 {o.side} {o.ticker} 数量 {o.quantity} 不是整手")
        if o.side == "SELL" and o.ticker not in positions:
            problems.append(f"挂单 SELL {o.ticker} 但没有持仓")
    if problems:
        raise StateError("；".join(problems))


# ---------------------------------------------------------------- 密钥遮蔽

_SECRET_NAMES = tuple(n for n in REQUIRED_ENV_VARS if n != "TELEGRAM_CHAT_ID")
# 就算不知道值，也遮掉看起来像 key / token 的字串
_PATTERNS = (
    re.compile(r"\b\d{6,}:[A-Za-z0-9_-]{30,}\b"),        # Telegram bot token
    re.compile(r"\bsb_(secret|publishable)_[A-Za-z0-9_-]{10,}"),
    re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+"),  # JWT
)


def secret_values(env=None) -> list[str]:
    env = os.environ if env is None else env
    return [v for n in _SECRET_NAMES if (v := str(env.get(n, "")).strip()) and len(v) >= 6]


def redact(text: str, secrets: Iterable[str] | None = None) -> str:
    text = str(text)
    for s in secrets if secrets is not None else secret_values():
        if s:
            text = text.replace(s, "***")
    for p in _PATTERNS:
        text = p.sub("***", text)
    return text


class RedactingFilter(logging.Filter):
    """装在 logging 上：所有 log 讯息都先遮蔽密钥。"""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            msg = record.getMessage()
        except Exception:  # noqa: BLE001
            return True
        record.msg, record.args = redact(msg), ()
        return True


def install_log_redaction() -> None:
    f = RedactingFilter()
    for h in logging.getLogger().handlers:
        h.addFilter(f)
