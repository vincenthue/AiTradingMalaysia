"""测试共用工具：全部用假数据，不连网络、不连真实数据库。"""
from __future__ import annotations

from dataclasses import replace
from datetime import date

import numpy as np
import pandas as pd
import pytest

from bot.config import load_settings


@pytest.fixture(autouse=True)
def _no_network_no_dotenv(monkeypatch):
    """测试绝不连真实网络、绝不读取真实 .env。"""
    import socket

    import dotenv

    def blocked(*a, **k):
        raise RuntimeError("测试中禁止连网络")

    monkeypatch.setattr(socket.socket, "connect", blocked)
    monkeypatch.setattr(socket, "create_connection", blocked)
    monkeypatch.setattr(dotenv, "load_dotenv", lambda *a, **k: False)


@pytest.fixture
def settings():
    return load_settings()


@pytest.fixture
def small_settings(settings):
    """短均线版本（5/10），方便构造交叉。"""
    return replace(settings, strategy=replace(settings.strategy, short_window=5, long_window=10))


def make_df(closes, end: date, opens=None, volumes=None) -> pd.DataFrame:
    """用收盘价序列造一个 OHLCV DataFrame，最后一天是 end（只用工作日）。"""
    closes = np.asarray(closes, dtype=float)
    n = len(closes)
    idx = pd.bdate_range(end=pd.Timestamp(end), periods=n)
    opens = closes if opens is None else np.asarray(opens, dtype=float)
    df = pd.DataFrame(
        {
            "Open": opens,
            "High": np.maximum(opens, closes),
            "Low": np.minimum(opens, closes),
            "Close": closes,
            "Volume": np.full(n, 1000.0) if volumes is None else np.asarray(volumes, dtype=float),
        },
        index=pd.Index([ts.date() for ts in idx], name="Date"),
    )
    return df


def golden_cross_closes(n_flat=15, base=10.0):
    """前面平稳下跌、最后一天大涨 → 5MA 上穿 10MA（最后一天才发生）。"""
    closes = [base - 0.01 * i for i in range(n_flat)]
    closes.append(closes[-1] * 1.2)
    return closes


def death_cross_closes(n_flat=15, base=10.0):
    closes = [base + 0.01 * i for i in range(n_flat)]
    closes.append(closes[-1] * 0.8)
    return closes
