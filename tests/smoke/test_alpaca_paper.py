"""The smoke test against the user's Alpaca paper account (`uv run pytest -m paper`).

It places a real, unfillable bracket order on the paper account and cancels it.
Skipped unless ALPACA_API_KEY and ALPACA_SECRET_KEY are set and the mode is paper.
"""

import os

import pytest

from trader_broker.brokers.alpaca import AlpacaBroker
from trader_broker.cli import DEFAULT_SMOKE_SYMBOL, run_smoke_test
from trader_broker.config import Settings

# Read at import: the autouse `clean_settings_env` fixture clears these before each test.
_ENV = dict(os.environ)
_MISSING_KEYS = [name for name in ("ALPACA_API_KEY", "ALPACA_SECRET_KEY") if not _ENV.get(name)]
_MODE = _ENV.get("TRADER_BROKER_MODE") or "paper"

pytestmark = [
    pytest.mark.paper,
    pytest.mark.skipif(bool(_MISSING_KEYS), reason=f"{' and '.join(_MISSING_KEYS)} not set"),
    pytest.mark.skipif(_MODE != "paper", reason=f"TRADER_BROKER_MODE is {_MODE}, not paper"),
]


def test_smoke_steps_pass_on_the_paper_account() -> None:
    broker = AlpacaBroker.from_settings(Settings.from_env(_ENV))

    checks = run_smoke_test(broker, DEFAULT_SMOKE_SYMBOL)

    for check in checks:  # shown with -s; records, among others, whether Alpaca returned legs
        print(f"{'PASS' if check.passed else 'FAIL'}  {check.name}: {check.detail}")
    assert [check.name for check in checks] == [
        "IEX quote",
        "SIP-history daily bars",
        "bracket buy accepted",
        "open orders list the legs with parent_id",
        "found by client order id",
        "cancel confirmed",
    ]
    assert [check for check in checks if not check.passed] == []
