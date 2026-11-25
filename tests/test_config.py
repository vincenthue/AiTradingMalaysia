import pytest
import yaml

from bot.config import (
    DEFAULT_SETTINGS_PATH,
    ConfigError,
    MissingEnvError,
    check_env,
    parse_settings,
)


def test_load_settings(settings):
    assert settings.initial_cash == 10000
    assert settings.benchmark == "^KLSE"
    assert settings.watchlist == ("1155.KL", "1295.KL", "1023.KL", "5347.KL", "6888.KL")
    assert settings.strategy.short_window == 50 and settings.strategy.long_window == 200
    assert settings.lot_size == 100
    assert settings.data.retry_delays_seconds == (5, 15, 45)


def test_bad_settings_rejected():
    raw = yaml.safe_load(DEFAULT_SETTINGS_PATH.read_text(encoding="utf-8"))
    raw["strategy"]["short_window"] = 300
    with pytest.raises(ConfigError):
        parse_settings(raw)


def test_missing_key_rejected():
    raw = yaml.safe_load(DEFAULT_SETTINGS_PATH.read_text(encoding="utf-8"))
    del raw["risk"]
    with pytest.raises(ConfigError, match="risk"):
        parse_settings(raw)


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
