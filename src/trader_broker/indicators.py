"""Technical indicators over price bars. Pure functions with no I/O.

Each takes bars oldest first and reads only the latest ones it needs. Each returns
None when there are fewer bars than its window needs, so a short history (a recent
listing, a thin data feed) reads as "unknown" rather than as a misleading number.
"""

from collections.abc import Sequence
from itertools import pairwise

from trader_broker.models import Bar

TRADING_DAYS_PER_YEAR = 252


def sma(bars: Sequence[Bar], period: int) -> float | None:
    """Simple moving average of the last `period` closes."""
    if len(bars) < period:
        return None
    return sum(float(bar.close) for bar in bars[-period:]) / period


def rsi(bars: Sequence[Bar], period: int = 14) -> float | None:
    """Relative strength index (Wilder's smoothing), from 0 to 100.

    Seeds the average gain and loss with the first `period` close-to-close changes,
    then smooths over the rest of the history, so it needs `period + 1` bars. With no
    losses it is 100; with no change at all it is 50.
    """
    if len(bars) < period + 1:
        return None
    changes = [float(cur.close - prev.close) for prev, cur in pairwise(bars)]
    avg_gain = _wilder_average([max(change, 0.0) for change in changes], period)
    avg_loss = _wilder_average([max(-change, 0.0) for change in changes], period)
    if avg_loss == 0:
        return 50.0 if avg_gain == 0 else 100.0
    return 100 - 100 / (1 + avg_gain / avg_loss)


def atr(bars: Sequence[Bar], period: int = 14) -> float | None:
    """Average true range (Wilder's smoothing), in price units.

    A bar's true range also spans the gap from the previous close, so it needs
    `period + 1` bars.
    """
    if len(bars) < period + 1:
        return None
    true_ranges = [
        max(
            float(cur.high - cur.low),
            abs(float(cur.high - prev.close)),
            abs(float(cur.low - prev.close)),
        )
        for prev, cur in pairwise(bars)
    ]
    return _wilder_average(true_ranges, period)


def pct_from_high(bars: Sequence[Bar], period: int = TRADING_DAYS_PER_YEAR) -> float | None:
    """Last close relative to the highest high of the last `period` bars, as a share.

    0.0 means at the high; -0.2 means 20% below it. The default period is the
    52-week high on daily bars.
    """
    if len(bars) < period:
        return None
    high = max(float(bar.high) for bar in bars[-period:])
    return float(bars[-1].close) / high - 1


def average_dollar_volume(bars: Sequence[Bar], period: int) -> float | None:
    """Mean of close * volume over the last `period` bars, in USD."""
    if len(bars) < period:
        return None
    return sum(float(bar.close) * bar.volume for bar in bars[-period:]) / period


def _wilder_average(values: list[float], period: int) -> float:
    """Mean of the first `period` values, then smoothed by each later value at 1/period."""
    average = sum(values[:period]) / period
    for value in values[period:]:
        average = (average * (period - 1) + value) / period
    return average
