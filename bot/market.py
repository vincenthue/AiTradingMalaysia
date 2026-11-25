"""市场相关的共用逻辑：时区、“今天”、收市前判断、货币格式、讯息标签。

所有“今天 / 交易日”的判断都用该市场自己的时区，绝不用服务器时间（UTC）
或马来西亚时间去算别的市场（PROJECT_SPEC 第 7.2 节）。
"""
from __future__ import annotations

from datetime import date, datetime
from zoneinfo import ZoneInfo

from bot.config import Settings


def local_now(st: Settings, now: datetime) -> datetime:
    """把任何带时区的时间转成该市场的当地时间。"""
    if now.tzinfo is None:
        raise ValueError("now 必须带时区（不能用本机时间去猜市场时区）")
    return now.astimezone(ZoneInfo(st.timezone))


def local_today(st: Settings, now: datetime) -> date:
    """该市场当地的“今天”。"""
    return local_now(st, now).date()


def before_close_cutoff(st: Settings, now: datetime) -> bool:
    """当地时间还没到 market_close_cutoff → True（这时的价格是盘中价，不能交易）。"""
    return local_now(st, now).time() < st.market_close_cutoff


def money(st: Settings, amount: float, signed: bool = False) -> str:
    """金额加上该市场的货币符号：RM 1,234.50 / $1,234.50；signed=True → +RM 5.00 / -$3.20。"""
    sym = st.currency_symbol
    sep = " " if sym[-1:].isalpha() else ""
    sign = ("+" if amount >= 0 else "-") if signed else ("-" if amount < 0 else "")
    return f"{sign}{sym}{sep}{abs(amount):,.2f}"


def price(st: Settings, value: float) -> str:
    """股价（同 money，但小于 1 的价格多显示一位，例如 RM 0.845）。"""
    sym = st.currency_symbol
    sep = " " if sym[-1:].isalpha() else ""
    digits = 3 if 0 < abs(value) < 1 else 2
    return f"{sym}{sep}{value:,.{digits}f}"


def tag(st: Settings) -> str:
    """讯息开头的市场标签，例如 🇲🇾 [MY]。"""
    return f"{st.flag} [{st.market}]".strip()
