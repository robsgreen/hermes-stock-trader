from datetime import date
from decimal import Decimal

from trader_broker.config import Settings
from trader_broker.risk import Caps, HaltCheck, check_halts

CAPS = Caps.from_settings(Settings())
TODAY = date(2026, 11, 3)
NEXT_TRADING_DAY = date(2026, 11, 4)


def _check(
    equity: int,
    last_equity: int = 100_000,
    peak: int = 100_000,
    kill_switch: bool = False,
    daily_halt_date: date | None = None,
    today: date = TODAY,
) -> HaltCheck:
    return check_halts(
        Decimal(equity),
        Decimal(last_equity),
        Decimal(peak),
        kill_switch,
        daily_halt_date,
        today,
        CAPS,
    )


def test_no_halt_within_limits() -> None:
    halts = _check(equity=97_000)

    assert halts.failures == []
    assert halts.daily_halt_date is None
    assert not halts.kill_switch


def test_daily_loss_over_the_limit_sets_the_daily_halt() -> None:
    halts = _check(equity=95_900)

    assert halts.daily_halt_date == TODAY
    assert halts.failures == ["daily_halt"]
    assert not halts.kill_switch


def test_daily_loss_at_the_limit_sets_the_daily_halt() -> None:
    assert _check(equity=96_000).daily_halt_date == TODAY


def test_daily_halt_keeps_blocking_today_after_recovery() -> None:
    halts = _check(equity=97_000, daily_halt_date=TODAY)

    assert halts.failures == ["daily_halt"]


def test_daily_halt_clears_on_the_next_trading_day() -> None:
    halts = _check(equity=97_000, last_equity=97_000, daily_halt_date=TODAY, today=NEXT_TRADING_DAY)

    assert halts.failures == []
    assert halts.daily_halt_date == TODAY


def test_drawdown_from_the_peak_sets_the_kill_switch() -> None:
    halts = _check(equity=95_900, last_equity=95_900, peak=120_000)

    assert halts.kill_switch
    assert halts.failures == ["kill_switch"]
    assert halts.peak == Decimal(120_000)


def test_peak_rises_with_equity() -> None:
    assert _check(equity=105_000).peak == Decimal(105_000)
    assert _check(equity=95_000, last_equity=95_000).peak == Decimal(100_000)


def test_kill_switch_stays_set_when_equity_recovers() -> None:
    halts = _check(equity=130_000, peak=120_000, kill_switch=True)

    assert halts.kill_switch
    assert halts.failures == ["kill_switch"]
    assert halts.peak == Decimal(130_000)


def test_both_halts_are_named() -> None:
    halts = _check(equity=79_000, last_equity=100_000)

    assert halts.failures == ["daily_halt", "kill_switch"]
    assert len(halts.reasons) == 2
