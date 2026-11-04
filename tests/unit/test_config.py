from pathlib import Path

import pytest

from trader_broker.config import LIVE_ACK_PHRASE, ConfigError, Settings


def test_defaults_load_in_paper_mode() -> None:
    settings = Settings.from_env()

    assert settings.mode == "paper"
    assert settings.state_dir == Path.home() / ".local" / "share" / "trader-broker"
    assert settings.risk_pct == 0.03
    assert settings.symbol_cap_pct == 0.20
    assert settings.daily_loss_pct == 0.04
    assert settings.kill_drawdown_pct == 0.20
    assert settings.min_price == 5
    assert settings.min_adv_usd == 20_000_000
    assert settings.entry_ttl_minutes == 30
    assert settings.entry_slippage_pct == 0.005
    assert settings.no_entry_before_close_minutes == 30
    assert settings.min_stop_pct == 0.005
    assert settings.max_stop_pct == 0.25
    assert settings.reviewer_timeout_s == 60
    assert settings.reviewer_1.provider == "openai"
    assert settings.reviewer_1.base_url == "https://api.openai.com/v1"
    assert settings.reviewer_1.api_key_env == "OPENAI_API_KEY"
    assert settings.reviewer_2.provider == "xai"
    assert settings.reviewer_2.base_url == "https://api.x.ai/v1"
    assert settings.reviewer_2.api_key_env == "XAI_API_KEY"
    assert settings.reviewer_1.model is None
    assert settings.alpaca_api_key is None


def test_reads_os_environ(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TRADER_RISK_PCT", "0.01")

    assert Settings.from_env().risk_pct == 0.01


def test_explicit_environ_ignores_os_environ(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TRADER_RISK_PCT", "0.01")

    assert Settings.from_env({}).risk_pct == 0.03


def test_live_without_ack_fails() -> None:
    with pytest.raises(ConfigError) as excinfo:
        Settings.from_env({"TRADER_BROKER_MODE": "live"})

    assert "TRADER_BROKER_MODE" in str(excinfo.value)
    assert "TRADER_BROKER_LIVE_ACK" in str(excinfo.value)


def test_live_with_wrong_ack_fails() -> None:
    env = {"TRADER_BROKER_MODE": "live", "TRADER_BROKER_LIVE_ACK": "yes"}

    with pytest.raises(ConfigError, match="TRADER_BROKER_LIVE_ACK"):
        Settings.from_env(env)


def test_live_with_exact_ack_loads() -> None:
    env = {"TRADER_BROKER_MODE": "live", "TRADER_BROKER_LIVE_ACK": LIVE_ACK_PHRASE}

    assert Settings.from_env(env).mode == "live"


def test_live_guard_also_applies_to_direct_construction() -> None:
    with pytest.raises(ValueError, match="TRADER_BROKER_LIVE_ACK"):
        Settings(mode="live")


def test_unknown_mode_fails() -> None:
    with pytest.raises(ConfigError, match="TRADER_BROKER_MODE"):
        Settings.from_env({"TRADER_BROKER_MODE": "sandbox"})


@pytest.mark.parametrize(
    "name",
    [
        "TRADER_RISK_PCT",
        "TRADER_SYMBOL_CAP_PCT",
        "TRADER_DAILY_LOSS_PCT",
        "TRADER_KILL_DRAWDOWN_PCT",
        "TRADER_ENTRY_SLIPPAGE_PCT",
        "TRADER_MIN_STOP_PCT",
        "TRADER_MAX_STOP_PCT",
    ],
)
@pytest.mark.parametrize("value", ["0", "1", "-0.1", "3"])
def test_pct_outside_zero_to_one_fails(name: str, value: str) -> None:
    with pytest.raises(ConfigError, match=name):
        Settings.from_env({name: value})


def test_unparsable_value_fails_naming_variable() -> None:
    with pytest.raises(ConfigError, match="TRADER_ENTRY_TTL_MINUTES"):
        Settings.from_env({"TRADER_ENTRY_TTL_MINUTES": "half an hour"})


@pytest.mark.parametrize("mode", ["paper", "live"])
def test_state_and_log_paths_contain_mode(mode: str, tmp_path: Path) -> None:
    env = {
        "TRADER_BROKER_MODE": mode,
        "TRADER_BROKER_LIVE_ACK": LIVE_ACK_PHRASE,
        "TRADER_BROKER_STATE_DIR": str(tmp_path),
    }

    settings = Settings.from_env(env)

    assert settings.state_db_path == tmp_path / mode / "state.db"
    assert settings.log_path == tmp_path / mode / "logs" / "trader-broker.log"


def test_state_dir_expands_home() -> None:
    settings = Settings.from_env({"TRADER_BROKER_STATE_DIR": "~/trader"})

    assert settings.state_dir == Path.home() / "trader"


def test_env_overrides_parse() -> None:
    env = {
        "TRADER_RISK_PCT": "0.02",
        "TRADER_SYMBOL_CAP_PCT": "0.1",
        "TRADER_DAILY_LOSS_PCT": "0.03",
        "TRADER_KILL_DRAWDOWN_PCT": "0.15",
        "TRADER_MIN_PRICE": "10",
        "TRADER_MIN_ADV_USD": "5000000",
        "TRADER_ENTRY_TTL_MINUTES": "15",
        "TRADER_ENTRY_SLIPPAGE_PCT": "0.01",
        "TRADER_NO_ENTRY_BEFORE_CLOSE_MINUTES": "45",
        "TRADER_MIN_STOP_PCT": "0.01",
        "TRADER_MAX_STOP_PCT": "0.1",
        "REVIEWER_TIMEOUT_S": "90",
        "REVIEWER_1_PROVIDER": "azure",
        "REVIEWER_1_BASE_URL": "https://example.test/v1",
        "REVIEWER_1_MODEL": "gpt-x",
        "REVIEWER_1_API_KEY_ENV": "AZURE_KEY",
        "AZURE_KEY": "azure-secret",
        "REVIEWER_2_MODEL": "grok-x",
        "ALPACA_API_KEY": "alpaca-key",
        "ALPACA_SECRET_KEY": "alpaca-secret",
    }

    settings = Settings.from_env(env)

    assert settings.risk_pct == 0.02
    assert settings.symbol_cap_pct == 0.1
    assert settings.daily_loss_pct == 0.03
    assert settings.kill_drawdown_pct == 0.15
    assert settings.min_price == 10
    assert settings.min_adv_usd == 5_000_000
    assert settings.entry_ttl_minutes == 15
    assert settings.entry_slippage_pct == 0.01
    assert settings.no_entry_before_close_minutes == 45
    assert settings.min_stop_pct == 0.01
    assert settings.max_stop_pct == 0.1
    assert settings.reviewer_timeout_s == 90
    assert settings.reviewer_1.provider == "azure"
    assert settings.reviewer_1.base_url == "https://example.test/v1"
    assert settings.reviewer_1.model == "gpt-x"
    assert settings.reviewer_1.api_key_env == "AZURE_KEY"
    assert settings.reviewer_1.api_key is not None
    assert settings.reviewer_1.api_key.get_secret_value() == "azure-secret"
    assert settings.reviewer_2.provider == "xai"
    assert settings.reviewer_2.model == "grok-x"
    assert settings.alpaca_api_key is not None
    assert settings.alpaca_api_key.get_secret_value() == "alpaca-key"
    assert settings.alpaca_secret_key is not None
    assert settings.alpaca_secret_key.get_secret_value() == "alpaca-secret"


def test_empty_values_keep_defaults() -> None:
    settings = Settings.from_env({"TRADER_BROKER_MODE": "", "TRADER_RISK_PCT": ""})

    assert settings.mode == "paper"
    assert settings.risk_pct == 0.03


def test_secrets_are_not_shown_in_repr() -> None:
    settings = Settings.from_env({"ALPACA_SECRET_KEY": "alpaca-secret", "OPENAI_API_KEY": "sk"})

    assert "alpaca-secret" not in repr(settings)


_REVIEWERS_READY = {
    "REVIEWER_1_MODEL": "gpt-x",
    "REVIEWER_2_MODEL": "grok-x",
    "OPENAI_API_KEY": "openai-secret",
    "XAI_API_KEY": "xai-secret",
}


def test_serve_check_passes_with_models_and_keys() -> None:
    Settings.from_env(_REVIEWERS_READY).require_reviewers()


def test_missing_reviewer_model_fails_serve_check() -> None:
    env = {**_REVIEWERS_READY}
    del env["REVIEWER_2_MODEL"]

    with pytest.raises(ConfigError, match="REVIEWER_2_MODEL"):
        Settings.from_env(env).require_reviewers()


def test_missing_reviewer_api_key_fails_serve_check() -> None:
    env = {**_REVIEWERS_READY}
    del env["OPENAI_API_KEY"]

    with pytest.raises(ConfigError, match="OPENAI_API_KEY"):
        Settings.from_env(env).require_reviewers()


def test_unresolved_placeholders_count_as_unset() -> None:
    """Hermes passes `${VAR}` through verbatim when VAR is missing from the profile's .env."""
    env = {
        **_REVIEWERS_READY,
        "TRADER_RISK_PCT": "${TRADER_RISK_PCT}",
        "REVIEWER_1_PROVIDER": "${REVIEWER_1_PROVIDER}",
        "OPENAI_API_KEY": "${OPENAI_API_KEY}",
    }

    settings = Settings.from_env(env)

    assert settings.risk_pct == 0.03
    assert settings.reviewer_1.provider == "openai"
    with pytest.raises(ConfigError, match="OPENAI_API_KEY"):
        settings.require_reviewers()


def test_values_merely_containing_a_dollar_brace_are_kept() -> None:
    settings = Settings.from_env({"ALPACA_SECRET_KEY": "ab${CD}"})

    assert settings.alpaca_secret_key is not None
    assert settings.alpaca_secret_key.get_secret_value() == "ab${CD}"
