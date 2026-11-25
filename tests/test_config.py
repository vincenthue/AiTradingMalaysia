import copy

import pytest
import yaml

from bot.config import (
    DEFAULT_MARKETS_PATH,
    ConfigError,
    MissingEnvError,
    available_markets,
    check_env,
    load_market,
    parse_market,
)


def _raw():
    return yaml.safe_load(DEFAULT_MARKETS_PATH.read_text(encoding="utf-8"))


def test_load_my_market(real_settings):
    st = real_settings
    assert st.market == "MY" and st.currency == "MYR" and st.currency_symbol == "RM"
    assert st.timezone == "Asia/Kuala_Lumpur"
    assert st.initial_cash > 0
    assert st.benchmark == "^KLSE"
    assert st.watchlist and all(t.endswith(".KL") for t in st.watchlist)
    assert len(set(st.watchlist)) == len(st.watchlist)
    assert 0 < st.risk.max_position_pct <= 1
    assert 0 <= st.risk.min_cash_reserve_pct < 1
    assert st.strategy.short_window == 50 and st.strategy.long_window == 200
    assert st.lot_size == 100
    assert st.data.retry_delays_seconds == (5, 15, 45)


def test_market_code_is_case_insensitive():
    assert load_market("my").market == "MY"


def test_every_market_in_yaml_loads():
    for code in available_markets():
        assert load_market(code).market == code


def test_unknown_market_rejected():
    with pytest.raises(ConfigError, match="没有市场"):
        load_market("XX")


def test_market_overrides_only_what_it_sets():
    """MY 覆盖 max_position_pct，其他风控项目沿用 defaults。"""
    raw = _raw()
    raw["markets"]["MY"]["risk"] = {"max_position_pct": 0.33}
    st = parse_market(raw, "MY")
    assert st.risk.max_position_pct == 0.33
    assert st.risk.max_positions == raw["defaults"]["risk"]["max_positions"]
    assert st.risk.min_cash_reserve_pct == raw["defaults"]["risk"]["min_cash_reserve_pct"]


def test_new_market_only_needs_yaml():
    """加一个新市场只需要在 markets.yaml 加一个区块（不用改代码）。"""
    raw = _raw()
    sg = copy.deepcopy(raw["markets"]["MY"])
    sg.update(name="Singapore", timezone="Asia/Singapore", currency="SGD", currency_symbol="S$",
              benchmark="^STI", watchlist=["D05.SI", "O39.SI"])
    sg.pop("risk", None)
    raw["markets"]["SG"] = sg
    st = parse_market(raw, "SG")
    assert st.market == "SG" and st.timezone == "Asia/Singapore"
    assert st.risk.max_position_pct == raw["defaults"]["risk"]["max_position_pct"]


def test_bad_settings_rejected():
    raw = _raw()
    raw["markets"]["MY"]["strategy"] = {"short_window": 300}
    with pytest.raises(ConfigError):
        parse_market(raw, "MY")


def test_bad_timezone_rejected():
    raw = _raw()
    raw["markets"]["MY"]["timezone"] = "Mars/Olympus"
    with pytest.raises(ConfigError, match="时区"):
        parse_market(raw, "MY")


def test_missing_key_rejected():
    raw = _raw()
    del raw["markets"]["MY"]["fees"]
    with pytest.raises(ConfigError, match="fees"):
        parse_market(raw, "MY")


def test_check_env_shows_names_not_values():
    env = {"SUPABASE_URL": "https://secret-value.example", "TELEGRAM_BOT_TOKEN": "  "}
    with pytest.raises(MissingEnvError) as ei:
        check_env(env=env)
    msg = str(ei.value)
    assert "SUPABASE_SERVICE_KEY" in msg and "TELEGRAM_BOT_TOKEN" in msg
    assert "TELEGRAM_CHAT_ID" in msg
    assert "SUPABASE_URL" not in ei.value.missing
    assert "secret-value" not in msg


def test_check_env_ok():
    env = {k: "x" for k in ("SUPABASE_URL", "SUPABASE_SERVICE_KEY", "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID")}
    assert check_env(env=env) == env
