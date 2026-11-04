"""Server settings, read once from environment variables at start.

Limits live here and nowhere else the agent can reach: no tool changes them, and the
server only re-reads them when it restarts.
"""

import os
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Annotated, Any, Literal, Self

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SecretStr,
    ValidationError,
    field_validator,
    model_validator,
)

LIVE_ACK_PHRASE = "I_ACCEPT_REAL_MONEY_RISK"
"""The exact `TRADER_BROKER_LIVE_ACK` value that allows live (real-money) mode."""

Fraction = Annotated[float, Field(gt=0, lt=1)]
"""A share of a whole, such as 0.03 for 3%, strictly between 0 and 1."""

_UNRESOLVED_PLACEHOLDER = re.compile(r"\$\{[A-Za-z_][A-Za-z0-9_]*\}")
"""A `${VAR}` reference passed through verbatim. Hermes does this for an MCP server env
value whose variable is unset or empty in the profile's .env."""

# Environment variable prefix and defaults for each reviewer slot.
_REVIEWER_ENV_PREFIXES = {"reviewer_1": "REVIEWER_1", "reviewer_2": "REVIEWER_2"}
_REVIEWER_DEFAULTS = {
    "reviewer_1": {
        "provider": "openai",
        "base_url": "https://api.openai.com/v1",
        "api_key_env": "OPENAI_API_KEY",
    },
    "reviewer_2": {
        "provider": "xai",
        "base_url": "https://api.x.ai/v1",
        "api_key_env": "XAI_API_KEY",
    },
}


class ConfigError(Exception):
    """The environment does not describe a configuration the server may start with."""


class ReviewerSettings(BaseModel):
    """One reviewer, reached through a direct API or tool-free native Hermes inference."""

    model_config = ConfigDict(frozen=True)

    provider: str
    transport: Literal["api", "hermes"] = "api"
    base_url: str
    model: str | None = None
    """Model id; there is no default, so the user picks it at install."""
    api_key_env: str
    """Name of the environment variable that holds this reviewer's API key."""
    api_key: SecretStr | None = None
    """Value of the `api_key_env` variable when settings were loaded, or None if unset."""


class Settings(BaseModel):
    """Every setting the server reads, keyed by its environment variable name.

    Construct with `Settings.from_env()`. Direct construction also accepts the
    field names and applies the same validation, including the live-mode guard.
    """

    model_config = ConfigDict(frozen=True, validate_by_alias=True, validate_by_name=True)

    mode: Literal["paper", "live"] = Field("paper", alias="TRADER_BROKER_MODE")
    live_ack: str | None = Field(None, alias="TRADER_BROKER_LIVE_ACK")
    state_dir: Path = Field(
        default_factory=lambda: Path.home() / ".local" / "share" / "trader-broker",
        alias="TRADER_BROKER_STATE_DIR",
    )
    account_lock_wait_s: float = Field(60, gt=0, alias="TRADER_ACCOUNT_LOCK_WAIT_S")
    """Seconds a `serve` tool call waits for another process's account lock before it
    returns `busy`. This wait plus the longest tool run (a buy's reviews) must stay
    under Hermes's MCP call timeout, or the agent sees a timeout while the queued call
    still runs."""

    risk_pct: Fraction = Field(0.03, alias="TRADER_RISK_PCT")
    """Share of equity a single trade may lose if its stop is hit."""
    symbol_cap_pct: Fraction = Field(0.20, alias="TRADER_SYMBOL_CAP_PCT")
    """Largest share of equity one symbol may take: the position's market value plus
    the unfilled part of its open buy orders."""
    daily_loss_pct: Fraction = Field(0.04, alias="TRADER_DAILY_LOSS_PCT")
    """Loss from the day's starting equity that halts new buys for the day."""
    kill_drawdown_pct: Fraction = Field(0.20, alias="TRADER_KILL_DRAWDOWN_PCT")
    """Drawdown from peak equity that trips the kill switch."""
    min_price: float = Field(5, gt=0, alias="TRADER_MIN_PRICE")
    """Lowest share price, in USD, the server will buy."""
    min_adv_usd: float = Field(20_000_000, gt=0, alias="TRADER_MIN_ADV_USD")
    """Lowest average daily dollar volume a symbol needs to be bought."""
    entry_ttl_minutes: int = Field(30, gt=0, alias="TRADER_ENTRY_TTL_MINUTES")
    """Minutes an unfilled entry order may rest before it is cancelled."""
    entry_slippage_pct: Fraction = Field(0.005, alias="TRADER_ENTRY_SLIPPAGE_PCT")
    """Allowed move above the quoted price when setting the entry limit."""
    no_entry_before_close_minutes: int = Field(
        30, ge=0, alias="TRADER_NO_ENTRY_BEFORE_CLOSE_MINUTES"
    )
    """Minutes before the close during which no new buy may start."""
    min_stop_pct: Fraction = Field(0.005, alias="TRADER_MIN_STOP_PCT")
    """Smallest allowed stop distance, as a share of the entry price."""
    max_stop_pct: Fraction = Field(0.25, alias="TRADER_MAX_STOP_PCT")
    """Largest allowed stop distance, as a share of the entry price."""

    reviewer_timeout_s: float = Field(60, gt=0, alias="REVIEWER_TIMEOUT_S")
    hermes_python: Path | None = Field(None, alias="TRADER_HERMES_PYTHON")
    hermes_home: Path | None = Field(None, alias="TRADER_HERMES_HOME")
    reviewer_1: ReviewerSettings = ReviewerSettings(**_REVIEWER_DEFAULTS["reviewer_1"])
    reviewer_2: ReviewerSettings = ReviewerSettings(**_REVIEWER_DEFAULTS["reviewer_2"])

    alpaca_api_key: SecretStr | None = Field(None, alias="ALPACA_API_KEY")
    alpaca_secret_key: SecretStr | None = Field(None, alias="ALPACA_SECRET_KEY")

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> Self:
        """Load settings from `environ` (default: `os.environ`).

        Empty values and unresolved `${VAR}` placeholders count as unset, so a blank
        line in an env template keeps the default and a missing reviewer key fails
        `require_reviewers` instead of being sent as a key. Raises ConfigError, naming
        the offending variables, when a value does not parse or is out of range, or
        when live mode lacks its acknowledgement.
        """
        source = os.environ if environ is None else environ
        env = {
            name: value
            for name, value in source.items()
            if value and not _UNRESOLVED_PLACEHOLDER.fullmatch(value)
        }
        aliases = {field.alias for field in cls.model_fields.values() if field.alias}
        values: dict[str, Any] = {name: env[name] for name in aliases if name in env}
        for field_name, prefix in _REVIEWER_ENV_PREFIXES.items():
            reviewer = dict(_REVIEWER_DEFAULTS[field_name])
            for key in ("provider", "base_url", "model", "api_key_env", "transport"):
                if f"{prefix}_{key.upper()}" in env:
                    reviewer[key] = env[f"{prefix}_{key.upper()}"]
            values[field_name] = {**reviewer, "api_key": env.get(reviewer["api_key_env"])}

        try:
            return cls.model_validate(values)
        except ValidationError as exc:
            raise ConfigError(_describe(exc)) from None

    @model_validator(mode="after")
    def _guard_live_mode(self) -> Self:
        if self.mode == "live" and self.live_ack != LIVE_ACK_PHRASE:
            raise ValueError(
                "TRADER_BROKER_MODE=live requires "
                f"TRADER_BROKER_LIVE_ACK={LIVE_ACK_PHRASE}; refusing to trade real money"
            )
        return self

    @field_validator("state_dir")
    @classmethod
    def _expand_home(cls, state_dir: Path) -> Path:
        return state_dir.expanduser()

    @property
    def state_db_path(self) -> Path:
        """SQLite state file; separate per mode so paper history never mixes with live."""
        return self.state_dir / self.mode / "state.db"

    @property
    def account_lock_path(self) -> Path:
        """Lock file that serializes broker writes across processes, separate per mode."""
        return self.state_dir / self.mode / "trader-broker.lock"

    @property
    def log_path(self) -> Path:
        """Server log file, separate per mode."""
        return self.state_dir / self.mode / "logs" / "trader-broker.log"

    def require_reviewers(self) -> None:
        """Require model ids plus API credentials or explicit Hermes runtime paths.

        `serve` calls this before starting, so a missing reviewer stops the server
        at start rather than blocking every buy later.
        """
        problems = []
        for field_name, prefix in _REVIEWER_ENV_PREFIXES.items():
            reviewer: ReviewerSettings = getattr(self, field_name)
            if reviewer.model is None:
                problems.append(f"{prefix}_MODEL is not set")
            if reviewer.transport == "hermes":
                if reviewer.provider not in {"openai-codex", "xai"}:
                    problems.append(f"{prefix}_PROVIDER must be openai-codex or xai for Hermes")
                python = self.hermes_python
                if (
                    python is None
                    or not python.is_absolute()
                    or not python.is_file()
                    or not os.access(python, os.X_OK)
                ):
                    problems.append("TRADER_HERMES_PYTHON must be an absolute executable file")
                home = self.hermes_home
                if home is None or not home.is_absolute() or not home.is_dir():
                    problems.append("TRADER_HERMES_HOME must be an absolute existing directory")
            elif reviewer.api_key is None:
                problems.append(
                    f"{reviewer.api_key_env} (named by {prefix}_API_KEY_ENV) is not set"
                )
        if problems:
            raise ConfigError("reviewers are not configured: " + "; ".join(problems))


def _describe(exc: ValidationError) -> str:
    """One line per validation error, led by the environment variable it concerns."""
    lines = ["invalid trader-broker settings:"]
    for error in exc.errors(include_url=False):
        location = ".".join(str(part) for part in error["loc"])
        if error["type"] == "value_error":
            lines.append(f"  {error['ctx']['error']}")
        else:
            lines.append(f"  {location}: {error['msg']} (got {error['input']!r})")
    return "\n".join(lines)
