"""读取配置（config/settings.yaml）和检查环境变量。

密钥只从环境变量读取；本模块报错时只显示变量名称，绝不显示值。
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

import yaml

PROJECT_NAME = "AiTradingMalaysia"
ROOT_DIR = Path(__file__).resolve().parent.parent
DEFAULT_SETTINGS_PATH = ROOT_DIR / "config" / "settings.yaml"

REQUIRED_ENV_VARS: tuple[str, ...] = (
    "SUPABASE_URL",
    "SUPABASE_SERVICE_KEY",
    "TELEGRAM_BOT_TOKEN",
    "TELEGRAM_CHAT_ID",
)


class ConfigError(Exception):
    """配置文件有问题。"""


class MissingEnvError(ConfigError):
    """缺少环境变量。只记录名称，不记录值。"""

    def __init__(self, missing: list[str]):
        self.missing = list(missing)
        super().__init__("缺少环境变量：" + ", ".join(self.missing))


@dataclass(frozen=True)
class StrategyConfig:
    name: str
    short_window: int
    long_window: int


@dataclass(frozen=True)
class RiskConfig:
    max_position_pct: float
    max_positions: int
    stop_loss_pct: float
    min_cash_reserve: float


@dataclass(frozen=True)
class FeeConfig:
    brokerage_pct: float
    brokerage_min: float
    clearing_pct: float
    stamp_duty_per_1000: float
    stamp_duty_max: float


@dataclass(frozen=True)
class DataConfig:
    history_period: str
    retry_delays_seconds: tuple[float, ...]
    max_daily_move_pct: float
    max_stale_days: int


@dataclass(frozen=True)
class Settings:
    initial_cash: float
    timezone: str
    benchmark: str
    watchlist: tuple[str, ...]
    strategy: StrategyConfig
    risk: RiskConfig
    fees: FeeConfig
    lot_size: int
    data: DataConfig


def _require(d: Mapping, key: str, where: str):
    if not isinstance(d, Mapping) or key not in d:
        raise ConfigError(f"settings.yaml 缺少 {where}{key}")
    return d[key]


def parse_settings(raw: Mapping) -> Settings:
    """把 YAML 内容转成 Settings，并做基本的合理性检查。"""
    s = _require(raw, "strategy", "")
    r = _require(raw, "risk", "")
    f = _require(raw, "fees", "")
    d = _require(raw, "data", "")

    watchlist = tuple(str(t).strip() for t in _require(raw, "watchlist", ""))
    settings = Settings(
        initial_cash=float(_require(raw, "initial_cash", "")),
        timezone=str(_require(raw, "timezone", "")),
        benchmark=str(_require(raw, "benchmark", "")),
        watchlist=watchlist,
        strategy=StrategyConfig(
            name=str(_require(s, "name", "strategy.")),
            short_window=int(_require(s, "short_window", "strategy.")),
            long_window=int(_require(s, "long_window", "strategy.")),
        ),
        risk=RiskConfig(
            max_position_pct=float(_require(r, "max_position_pct", "risk.")),
            max_positions=int(_require(r, "max_positions", "risk.")),
            stop_loss_pct=float(_require(r, "stop_loss_pct", "risk.")),
            min_cash_reserve=float(_require(r, "min_cash_reserve", "risk.")),
        ),
        fees=FeeConfig(
            brokerage_pct=float(_require(f, "brokerage_pct", "fees.")),
            brokerage_min=float(_require(f, "brokerage_min", "fees.")),
            clearing_pct=float(_require(f, "clearing_pct", "fees.")),
            stamp_duty_per_1000=float(_require(f, "stamp_duty_per_1000", "fees.")),
            stamp_duty_max=float(_require(f, "stamp_duty_max", "fees.")),
        ),
        lot_size=int(_require(raw, "lot_size", "")),
        data=DataConfig(
            history_period=str(_require(d, "history_period", "data.")),
            retry_delays_seconds=tuple(
                float(x) for x in _require(d, "retry_delays_seconds", "data.")
            ),
            max_daily_move_pct=float(_require(d, "max_daily_move_pct", "data.")),
            max_stale_days=int(_require(d, "max_stale_days", "data.")),
        ),
    )
    _sanity_check(settings)
    return settings


def _sanity_check(st: Settings) -> None:
    errors = []
    if st.initial_cash <= 0:
        errors.append("initial_cash 必须 > 0")
    if not st.watchlist:
        errors.append("watchlist 不能是空的")
    if len(set(st.watchlist)) != len(st.watchlist):
        errors.append("watchlist 有重复的代码")
    if st.strategy.name != "ma_crossover":
        errors.append(f"不支持的策略：{st.strategy.name}")
    if not (0 < st.strategy.short_window < st.strategy.long_window):
        errors.append("需要 0 < short_window < long_window")
    if not (0 < st.risk.max_position_pct <= 1):
        errors.append("max_position_pct 必须在 (0, 1]")
    if st.risk.max_positions < 1:
        errors.append("max_positions 必须 >= 1")
    if not (0 < st.risk.stop_loss_pct < 1):
        errors.append("stop_loss_pct 必须在 (0, 1)")
    if st.risk.min_cash_reserve < 0:
        errors.append("min_cash_reserve 不能是负数")
    if st.lot_size < 1:
        errors.append("lot_size 必须 >= 1")
    if any(x < 0 for x in st.data.retry_delays_seconds):
        errors.append("retry_delays_seconds 不能有负数")
    if errors:
        raise ConfigError("settings.yaml 有问题：" + "；".join(errors))


def load_settings(path: str | Path = DEFAULT_SETTINGS_PATH) -> Settings:
    path = Path(path)
    if not path.exists():
        raise ConfigError(f"找不到配置文件：{path}")
    with path.open(encoding="utf-8") as fh:
        raw = yaml.safe_load(fh)
    return parse_settings(raw or {})


def check_env(
    required: tuple[str, ...] = REQUIRED_ENV_VARS,
    env: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """检查环境变量是否齐全。缺少就抛 MissingEnvError（只含名称）。"""
    env = os.environ if env is None else env
    missing = [name for name in required if not str(env.get(name, "")).strip()]
    if missing:
        raise MissingEnvError(missing)
    return {name: env[name] for name in required}
