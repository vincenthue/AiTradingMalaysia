"""读取多市场配置（config/markets.yaml）和检查环境变量。

密钥只从环境变量读取；本模块报错时只显示变量名称，绝不显示值。
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass
from datetime import time
from pathlib import Path
from typing import Mapping, Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import yaml

PROJECT_NAME = "AiTradingMalaysia"
ROOT_DIR = Path(__file__).resolve().parent.parent
DEFAULT_MARKETS_PATH = ROOT_DIR / "config" / "markets.yaml"
MARKET_CODE_RE = re.compile(r"[A-Z]{2,5}")

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
    min_cash_reserve_pct: float   # 买入后现金至少 = 总资产 × 这个比例


@dataclass(frozen=True)
class FeeConfig:
    """费用参数。市场没写的项目 = 0（不收）；计算见 bot/fees.py。"""
    brokerage_pct: float = 0.0
    brokerage_min: float = 0.0
    clearing_pct: float = 0.0
    stamp_duty_per_1000: float = 0.0
    stamp_duty_max: Optional[float] = None    # None = 没有上限
    sec_fee_pct_on_sell: float = 0.0          # 美股 SEC fee：只在卖出时收


FEE_KEYS = tuple(f for f in FeeConfig.__dataclass_fields__)


def _parse_fees(f: Mapping, where: str) -> FeeConfig:
    if not isinstance(f, Mapping) or not f:
        raise ConfigError(f"{where}fees 必须列出至少一个费用项目")
    unknown = sorted(set(f) - set(FEE_KEYS))
    if unknown:
        raise ConfigError(f"{where}fees 有不认识的项目：{', '.join(unknown)}（可用：{', '.join(FEE_KEYS)}）")
    values = {}
    for k, v in f.items():
        try:
            values[k] = None if v is None else float(v)
        except (TypeError, ValueError):
            raise ConfigError(f"{where}fees.{k} 必须是数字") from None
        if values[k] is not None and values[k] < 0:
            raise ConfigError(f"{where}fees.{k} 不能是负数")
    return FeeConfig(**values)


@dataclass(frozen=True)
class DataConfig:
    history_period: str
    retry_delays_seconds: tuple[float, ...]
    max_daily_move_pct: float
    max_stale_days: int


@dataclass(frozen=True)
class Settings:
    """一个市场的完整设定（markets.yaml 的 defaults 已合并进来）。"""
    market: str               # 市场代码，例如 MY / US
    name: str
    flag: str
    currency: str             # MYR / USD
    currency_symbol: str      # RM / $
    timezone_label: str       # 讯息里显示的“当地时间”名称
    initial_cash: float
    timezone: str
    market_close_cutoff: time
    benchmark: str
    watchlist: tuple[str, ...]
    strategy: StrategyConfig
    risk: RiskConfig
    fees: FeeConfig
    lot_size: int
    data: DataConfig


def _require(d: Mapping, key: str, where: str):
    if not isinstance(d, Mapping) or key not in d:
        raise ConfigError(f"markets.yaml 缺少 {where}{key}")
    return d[key]


def _parse_hhmm(value) -> time:
    try:
        hh, mm = str(value).strip().split(":")
        return time(int(hh), int(mm))
    except (ValueError, TypeError):
        raise ConfigError(f"market_close_cutoff 格式要是 \"HH:MM\"，现在是 {value!r}") from None


def _merged(defaults: Mapping, block: Mapping, key: str) -> dict:
    """defaults.<key> 先，再用市场区块的 <key> 覆盖写到的项目。"""
    base = dict(defaults.get(key) or {}) if isinstance(defaults, Mapping) else {}
    override = block.get(key) or {}
    if not isinstance(override, Mapping):
        raise ConfigError(f"markets.yaml 的 {key} 必须是一组设定")
    base.update(override)
    return base


def market_codes(raw: Mapping) -> tuple[str, ...]:
    markets = _require(raw, "markets", "")
    if not isinstance(markets, Mapping) or not markets:
        raise ConfigError("markets.yaml 的 markets 不能是空的")
    return tuple(str(k) for k in markets)


def parse_market(raw: Mapping, code: str) -> Settings:
    """把 markets.yaml 里某个市场（合并 defaults 之后）转成 Settings，并做合理性检查。"""
    codes = market_codes(raw)
    if code not in codes:
        raise ConfigError(f"markets.yaml 没有市场 {code!r}（可用：{', '.join(codes)}）")
    m = raw["markets"][code]
    if not isinstance(m, Mapping):
        raise ConfigError(f"markets.{code} 必须是一组设定")
    defaults = raw.get("defaults") or {}
    w = f"markets.{code}."
    s = _merged(defaults, m, "strategy")
    r = _merged(defaults, m, "risk")
    d = _merged(defaults, m, "data")
    f = _require(m, "fees", w)

    watchlist = tuple(str(t).strip() for t in _require(m, "watchlist", w) or ())
    settings = Settings(
        market=code,
        name=str(_require(m, "name", w)),
        flag=str(m.get("flag", "")),
        currency=str(_require(m, "currency", w)),
        currency_symbol=str(_require(m, "currency_symbol", w)),
        timezone_label=str(m.get("timezone_label") or _require(m, "timezone", w)),
        initial_cash=float(_require(m, "initial_cash", w)),
        timezone=str(_require(m, "timezone", w)),
        market_close_cutoff=_parse_hhmm(_require(m, "market_close_cutoff", w)),
        benchmark=str(_require(m, "benchmark", w)),
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
            min_cash_reserve_pct=float(_require(r, "min_cash_reserve_pct", "risk.")),
        ),
        fees=_parse_fees(f, w),
        lot_size=int(_require(m, "lot_size", w)),
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
    if not MARKET_CODE_RE.fullmatch(st.market):
        errors.append("市场代码要是 2～5 个大写英文字母（例如 MY、US）")
    try:
        ZoneInfo(st.timezone)
    except (ZoneInfoNotFoundError, ValueError):
        errors.append(f"不认识的时区：{st.timezone}")
    if st.initial_cash <= 0:
        errors.append("initial_cash 必须 > 0")
    if not st.watchlist:
        errors.append("watchlist 不能是空的")
    if len(set(st.watchlist)) != len(st.watchlist):
        errors.append("watchlist 有重复的代码")
    if st.benchmark in st.watchlist:
        errors.append("benchmark 不能同时在 watchlist 里")
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
    if not (0 <= st.risk.min_cash_reserve_pct < 1):
        errors.append("min_cash_reserve_pct 必须在 [0, 1)")
    if st.lot_size < 1:
        errors.append("lot_size 必须 >= 1")
    if any(x < 0 for x in st.data.retry_delays_seconds):
        errors.append("retry_delays_seconds 不能有负数")
    if errors:
        raise ConfigError(f"markets.yaml 的 {st.market} 有问题：" + "；".join(errors))


def _read_yaml(path: str | Path) -> Mapping:
    path = Path(path)
    if not path.exists():
        raise ConfigError(f"找不到配置文件：{path}")
    with path.open(encoding="utf-8") as fh:
        raw = yaml.safe_load(fh)
    return raw or {}


def available_markets(path: str | Path = DEFAULT_MARKETS_PATH) -> tuple[str, ...]:
    return market_codes(_read_yaml(path))


def load_market(code: str, path: str | Path = DEFAULT_MARKETS_PATH) -> Settings:
    """读取某个市场的完整设定（已合并 defaults）。"""
    return parse_market(_read_yaml(path), str(code).strip().upper())


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
