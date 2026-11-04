from datetime import UTC, datetime
from decimal import Decimal

import pytest

from trader_broker.indicators import atr, average_dollar_volume, pct_from_high, rsi, sma
from trader_broker.models import Bar

_TIMESTAMP = datetime(2026, 1, 1, tzinfo=UTC)


def _bar(close: float, high: float | None = None, low: float | None = None, volume: int = 0) -> Bar:
    return Bar(
        timestamp=_TIMESTAMP,
        open=Decimal(str(close)),
        high=Decimal(str(close if high is None else high)),
        low=Decimal(str(close if low is None else low)),
        close=Decimal(str(close)),
        volume=volume,
    )


def _closes(*closes: float) -> list[Bar]:
    return [_bar(close) for close in closes]


def test_sma_averages_the_latest_closes() -> None:
    assert sma(_closes(1, 2, 3, 4, 5), 3) == pytest.approx(4.0)


def test_sma_needs_a_full_window() -> None:
    assert sma(_closes(1, 2), 3) is None


def test_rsi_seeds_with_simple_averages() -> None:
    # 10 rises of 1 then 4 falls of 1: average gain 10/14, loss 4/14, RS 2.5.
    closes = [10 + i for i in range(11)] + [19, 18, 17, 16]

    assert rsi(_closes(*closes)) == pytest.approx(100 - 100 / 3.5)


def test_rsi_smooths_later_changes() -> None:
    # One more rise of 2: gain (10/14*13 + 2)/14 = 158/196, loss (4/14*13)/14 = 52/196.
    closes = [10 + i for i in range(11)] + [19, 18, 17, 16, 18]

    assert rsi(_closes(*closes)) == pytest.approx(100 - 100 / (1 + 158 / 52))


def test_rsi_is_100_without_losses_and_50_without_change() -> None:
    assert rsi(_closes(*range(1, 16))) == 100.0
    assert rsi(_closes(*[5] * 15)) == 50.0


def test_rsi_needs_period_plus_one_bars() -> None:
    assert rsi(_closes(*range(1, 15))) is None


def _atr_bars() -> list[Bar]:
    # A first close of 10, then 14 bars of range 9..11 closing at 10: every true range is 2.
    return [_bar(10)] + [_bar(10, high=11, low=9) for _ in range(14)]


def test_atr_seeds_with_mean_true_range() -> None:
    assert atr(_atr_bars()) == pytest.approx(2.0)


def test_atr_true_range_spans_gaps_and_smooths() -> None:
    # Gap up from 10 to a 13..15 bar: true range 15 - 10 = 5; ATR (2*13 + 5)/14.
    bars = [*_atr_bars(), _bar(14, high=15, low=13)]

    assert atr(bars) == pytest.approx(31 / 14)


def test_atr_needs_period_plus_one_bars() -> None:
    assert atr(_atr_bars()[:14]) is None


def test_pct_from_high_uses_the_last_52_weeks() -> None:
    # A 200 high just outside the 252-bar window is ignored; the 125 high inside counts.
    bars = [_bar(100, high=200)] + [_bar(100, high=125)] + [_bar(100)] * 251

    assert pct_from_high(bars) == pytest.approx(-0.2)


def test_pct_from_high_is_zero_at_the_high() -> None:
    assert pct_from_high([_bar(100)] * 252) == pytest.approx(0.0)


def test_pct_from_high_needs_a_full_year() -> None:
    assert pct_from_high([_bar(100)] * 251) is None


def test_average_dollar_volume_uses_close_times_volume() -> None:
    bars = [_bar(50, volume=999), _bar(10, volume=100), _bar(20, volume=200)]

    # (10*100 + 20*200) / 2
    assert average_dollar_volume(bars, 2) == pytest.approx(2500.0)


def test_average_dollar_volume_needs_a_full_window() -> None:
    assert average_dollar_volume([_bar(10, volume=100)], 2) is None
