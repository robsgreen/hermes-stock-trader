"""AlpacaBroker against stubbed alpaca-py clients: request shapes, error mapping, retries.

No test here touches the network; the clients are mocks with the SDK clients' specs.
"""

import json
import uuid
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock
from zoneinfo import ZoneInfo

import pytest
import requests
from alpaca.common.exceptions import APIError
from alpaca.data.enums import DataFeed
from alpaca.data.historical.news import NewsClient
from alpaca.data.historical.screener import ScreenerClient
from alpaca.data.historical.stock import StockHistoricalDataClient
from alpaca.data.models.bars import BarSet
from alpaca.data.models.screener import Mover
from alpaca.trading.client import TradingClient
from alpaca.trading.models import Calendar, PortfolioHistory
from alpaca.trading.models import Order as SdkOrder

from trader_broker.brokers import BrokerError, RateLimitError, UnknownResultError
from trader_broker.brokers import alpaca as alpaca_module
from trader_broker.brokers.alpaca import AlpacaBroker
from trader_broker.config import ConfigError, Settings
from trader_broker.models import BracketOrderRequest

_NEW_YORK = ZoneInfo("America/New_York")
_ENTRY_ID = "61e69015-8549-4bfd-b9c3-01e75843f47d"


@pytest.fixture
def trading() -> MagicMock:
    return MagicMock(spec=TradingClient)


@pytest.fixture
def stock_data() -> MagicMock:
    return MagicMock(spec=StockHistoricalDataClient)


@pytest.fixture
def broker(trading: MagicMock, stock_data: MagicMock) -> AlpacaBroker:
    return AlpacaBroker(
        trading, stock_data, MagicMock(spec=NewsClient), MagicMock(spec=ScreenerClient)
    )


@pytest.fixture
def sleeps(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """Pauses the adapter asked for; it never really sleeps."""
    pauses: list[float] = []
    monkeypatch.setattr(alpaca_module, "sleep", pauses.append)
    return pauses


def _api_error(status: int, message: str = "refused") -> APIError:
    response = requests.Response()
    response.status_code = status
    body = json.dumps({"code": status * 100_000, "message": message})
    return APIError(body, requests.HTTPError(response=response))


def _order_json(**fields: Any) -> dict[str, Any]:
    """An order as Alpaca's API returns it, with string numbers."""
    stamp = "2026-11-03T15:30:00.123456Z"
    return {
        "id": _ENTRY_ID,
        "client_order_id": "tb-1",
        "created_at": stamp,
        "updated_at": stamp,
        "submitted_at": stamp,
        "symbol": "XYZ",
        "asset_class": "us_equity",
        "qty": "10",
        "filled_qty": "0",
        "filled_avg_price": None,
        "order_class": "bracket",
        "type": "limit",
        "side": "buy",
        "time_in_force": "gtc",
        "limit_price": "100.5",
        "stop_price": None,
        "status": "new",
        "extended_hours": False,
        "legs": None,
        **fields,
    }


def _bracket_json() -> dict[str, Any]:
    """A nested bracket whose entry has filled: take-profit working, stop held."""
    take_profit = _order_json(
        id=str(uuid.uuid4()),
        client_order_id="leg-tp",
        side="sell",
        limit_price="120",
        status="new",
    )
    stop = _order_json(
        id=str(uuid.uuid4()),
        client_order_id="leg-stop",
        side="sell",
        type="stop",
        limit_price=None,
        stop_price="90",
        status="held",
    )
    return _order_json(
        status="filled",
        filled_qty="10",
        filled_avg_price="100.45",
        legs=[take_profit, stop],
    )


def _bracket_request(**fields: Any) -> BracketOrderRequest:
    values: dict[str, Any] = {
        "symbol": "XYZ",
        "qty": 10,
        "limit_price": Decimal("100.5"),
        "stop_price": Decimal("90.004"),
        "take_profit_price": Decimal("120.125"),
        "client_order_id": "tb-1",
        **fields,
    }
    return BracketOrderRequest(**values)


def _account() -> SimpleNamespace:
    return SimpleNamespace(
        equity="100000.5", last_equity="99000", cash="50000", buying_power="200000"
    )


# ----- Construction -----


def test_from_settings_requires_both_alpaca_keys() -> None:
    with pytest.raises(ConfigError, match="ALPACA_SECRET_KEY"):
        AlpacaBroker.from_settings(Settings(ALPACA_API_KEY="key"))


def test_from_settings_uses_paper_endpoint_and_takes_over_sdk_retries() -> None:
    broker = AlpacaBroker.from_settings(Settings(ALPACA_API_KEY="key", ALPACA_SECRET_KEY="secret"))

    assert "paper-api" in broker._trading._base_url
    clients = (broker._trading, broker._stock_data, broker._news, broker._screener)
    assert {client._retry for client in clients} == {0}
    assert {client._session.request.keywords["timeout"] for client in clients} == {20}


# ----- Writes -----


def test_bracket_buy_sends_string_prices_rounded_to_the_cent(
    broker: AlpacaBroker, trading: MagicMock
) -> None:
    trading.post.return_value = _order_json()

    entry = broker.submit_bracket_buy(_bracket_request())

    trading.post.assert_called_once_with(
        "/orders",
        {
            "symbol": "XYZ",
            "qty": "10",
            "side": "buy",
            "type": "limit",
            "order_class": "bracket",
            "time_in_force": "gtc",
            "limit_price": "100.50",
            "take_profit": {"limit_price": "120.13"},
            "stop_loss": {"stop_price": "90.00"},
            "client_order_id": "tb-1",
        },
    )
    assert (entry.id, entry.client_order_id, entry.parent_id) == (_ENTRY_ID, "tb-1", None)
    assert (entry.order_class, entry.type, entry.limit_price) == (
        "bracket",
        "limit",
        Decimal("100.5"),
    )


def test_sell_and_stop_requests(broker: AlpacaBroker, trading: MagicMock) -> None:
    trading.post.return_value = _order_json(order_class="simple")

    broker.submit_sell("XYZ", 5, "tb-sell")
    broker.submit_stop("XYZ", 5, Decimal("95.1"), "tb-stop")

    sell, stop = (call.args[1] for call in trading.post.call_args_list)
    assert sell == {
        "symbol": "XYZ",
        "qty": "5",
        "side": "sell",
        "type": "market",
        "time_in_force": "day",
        "client_order_id": "tb-sell",
    }
    assert stop == {
        "symbol": "XYZ",
        "qty": "5",
        "side": "sell",
        "type": "stop",
        "stop_price": "95.10",
        "time_in_force": "gtc",
        "client_order_id": "tb-stop",
    }


def test_replace_stop_sends_the_client_order_id_it_is_given(
    broker: AlpacaBroker, trading: MagicMock
) -> None:
    trading.patch.return_value = _order_json(
        type="stop", limit_price=None, stop_price="96", client_order_id="tb-raised"
    )

    replacement = broker.replace_stop(_ENTRY_ID, Decimal(96), "tb-raised")

    path, payload = trading.patch.call_args.args
    assert path == f"/orders/{_ENTRY_ID}"
    assert payload == {"stop_price": "96.00", "client_order_id": "tb-raised"}
    assert replacement.stop_price == Decimal(96)


def test_rate_limited_write_is_not_retried(
    broker: AlpacaBroker, trading: MagicMock, sleeps: list[float]
) -> None:
    trading.post.side_effect = _api_error(429)

    with pytest.raises(RateLimitError):
        broker.submit_bracket_buy(_bracket_request())

    assert trading.post.call_count == 1
    assert sleeps == []


@pytest.mark.parametrize(
    "failure",
    [requests.exceptions.ReadTimeout("read timed out"), requests.ConnectionError("reset")],
)
def test_write_timeout_raises_unknown_result(
    broker: AlpacaBroker, trading: MagicMock, failure: Exception
) -> None:
    trading.post.side_effect = failure

    with pytest.raises(UnknownResultError):
        broker.submit_bracket_buy(_bracket_request())

    assert trading.post.call_count == 1


def test_write_server_error_is_unknown_but_refusal_is_not(
    broker: AlpacaBroker, trading: MagicMock
) -> None:
    trading.post.side_effect = _api_error(504)
    with pytest.raises(UnknownResultError):
        broker.submit_sell("XYZ", 1, "tb-a")

    trading.post.side_effect = _api_error(403, "insufficient qty available for order")
    with pytest.raises(BrokerError, match="insufficient qty") as refused:
        broker.submit_sell("XYZ", 1, "tb-b")
    assert not isinstance(refused.value, UnknownResultError)


def test_cancel_rejects_malformed_order_id_without_a_request(
    broker: AlpacaBroker, trading: MagicMock
) -> None:
    with pytest.raises(BrokerError, match="not an Alpaca order id"):
        broker.cancel_order("order-1")

    trading.cancel_order_by_id.assert_not_called()


# ----- Reads -----


def test_rate_limited_read_retries_with_backoff_then_succeeds(
    broker: AlpacaBroker, trading: MagicMock, sleeps: list[float]
) -> None:
    trading.get_account.side_effect = [_api_error(429), _api_error(429), _account()]

    account = broker.get_account()

    assert account.equity == Decimal("100000.5")
    assert account.last_equity == Decimal(99000)
    assert sleeps == [1, 2]


def test_read_gives_up_after_three_retries(
    broker: AlpacaBroker, trading: MagicMock, sleeps: list[float]
) -> None:
    trading.get_account.side_effect = _api_error(429)

    with pytest.raises(RateLimitError):
        broker.get_account()

    assert trading.get_account.call_count == 4
    assert sleeps == [1, 2, 4]


def test_read_failure_other_than_rate_limit_is_not_retried(
    broker: AlpacaBroker, trading: MagicMock, sleeps: list[float]
) -> None:
    trading.get_account.side_effect = requests.exceptions.ReadTimeout("read timed out")

    with pytest.raises(BrokerError) as raised:
        broker.get_account()

    assert not isinstance(raised.value, UnknownResultError)
    assert trading.get_account.call_count == 1
    assert sleeps == []


def test_order_by_client_id_not_found_is_none(broker: AlpacaBroker, trading: MagicMock) -> None:
    trading.get_order_by_client_id.side_effect = _api_error(404, "order not found")

    assert broker.get_order_by_client_id("tb-missing") is None


def test_order_by_client_id_other_failures_raise(broker: AlpacaBroker, trading: MagicMock) -> None:
    trading.get_order_by_client_id.side_effect = _api_error(500)

    with pytest.raises(BrokerError):
        broker.get_order_by_client_id("tb-1")


def test_unknown_asset_is_none(broker: AlpacaBroker, trading: MagicMock) -> None:
    trading.get_asset.side_effect = _api_error(404, "asset not found")

    assert broker.get_asset("NOPE") is None


def test_orders_are_flattened_with_legs_pointing_at_their_entry(
    broker: AlpacaBroker, trading: MagicMock
) -> None:
    trading.get_orders.return_value = [SdkOrder(**_bracket_json())]

    every = broker.get_orders("all")
    working = broker.get_orders("open")

    assert [o.client_order_id for o in every] == ["tb-1", "leg-tp", "leg-stop"]
    assert [o.parent_id for o in every] == [None, _ENTRY_ID, _ENTRY_ID]
    assert every[0].filled_avg_price == Decimal("100.45")
    assert [(o.type, o.status) for o in working] == [("limit", "new"), ("stop", "held")]


def test_open_orders_merge_alpacas_open_list_with_the_newest_orders_of_every_status(
    broker: AlpacaBroker, trading: MagicMock
) -> None:
    # An old GTC stop, past the newest 500 orders, is only on Alpaca's open list; the
    # working legs of a filled entry are judged by their own status in either list.
    old_stop = _order_json(
        id=str(uuid.uuid4()),
        client_order_id="tb-old-stop",
        created_at="2026-09-01T15:30:00Z",
        order_class="simple",
        side="sell",
        type="stop",
        limit_price=None,
        stop_price="80",
    )

    def orders(request: Any) -> list[SdkOrder]:
        if request.status.value == "open":
            return [SdkOrder(**_bracket_json()), SdkOrder(**old_stop)]
        return [SdkOrder(**_bracket_json())]

    trading.get_orders.side_effect = orders

    working = broker.get_orders("open", limit=10)

    assert [o.client_order_id for o in working] == ["leg-tp", "leg-stop", "tb-old-stop"]
    requests_sent = [call.args[0] for call in trading.get_orders.call_args_list]
    assert sorted((r.status.value, r.nested, r.limit) for r in requests_sent) == [
        ("all", True, 500),
        ("open", True, 500),
    ]


def _fill_json(number: int, when: datetime) -> dict[str, Any]:
    return {
        "id": f"{when:%Y%m%d%H%M%S}::fill-{number}",
        "account_id": str(uuid.uuid4()),
        "activity_type": "FILL",
        "transaction_time": when.isoformat(),
        "type": "fill",
        "price": "101.5",
        "qty": "2",
        "side": "sell",
        "symbol": "XYZ",
        "leaves_qty": "0",
        "order_id": _ENTRY_ID,
        "cum_qty": "2",
        "order_status": "filled",
    }


def test_fills_are_read_page_by_page_until_a_short_page(
    broker: AlpacaBroker, trading: MagicMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(alpaca_module, "_FILLS_PAGE_SIZE", 2)
    since = datetime(2026, 11, 3, 15, 0, tzinfo=UTC)
    stamps = [since - timedelta(milliseconds=500), since, since + timedelta(minutes=1)]
    pages = [[_fill_json(0, stamps[0]), _fill_json(1, stamps[1])], [_fill_json(2, stamps[2])]]
    trading.get.side_effect = pages

    fills = broker.get_fills(since)

    # The fill half a second before `since` is on the first page but filtered out.
    assert [fill.filled_at for fill in fills] == stamps[1:]
    assert (fills[0].qty, fills[0].price, fills[0].side) == (2, Decimal("101.5"), "sell")
    first, second = (call.args for call in trading.get.call_args_list)
    assert first[0] == "/account/activities/FILL"
    assert first[1]["after"] == (since - timedelta(seconds=1)).isoformat()
    assert (first[1]["direction"], first[1]["page_size"]) == ("asc", 2)
    assert "page_token" not in first[1]
    assert second[1]["page_token"] == pages[0][-1]["id"]


def test_portfolio_history_maps_daily_equity_from_a_new_york_start(
    broker: AlpacaBroker, trading: MagicMock
) -> None:
    trading.get_portfolio_history.return_value = PortfolioHistory(
        timestamp=[1_793_768_400, 1_793_854_800],
        equity=[100_000.0, 100_250.5],
        profit_loss=[0.0, 250.5],
        profit_loss_pct=[0.0, 0.0025],
        base_value=100_000.0,
        timeframe="1D",
        cashflow={},
    )

    points = broker.get_portfolio_history(date(2026, 11, 2))

    request = trading.get_portfolio_history.call_args.args[0]
    assert request.timeframe == "1D"
    assert request.start == datetime(2026, 11, 2, 5, tzinfo=UTC)
    assert [(point.timestamp, point.equity) for point in points] == [
        (datetime.fromtimestamp(1_793_768_400, UTC), Decimal(100_000)),
        (datetime.fromtimestamp(1_793_854_800, UTC), Decimal("100250.5")),
    ]


def test_mover_change_pct_is_a_fraction(broker: AlpacaBroker) -> None:
    screener = broker._screener
    assert isinstance(screener, MagicMock)
    screener.get_market_movers.return_value = SimpleNamespace(
        gainers=[Mover(symbol="XYZ", percent_change=5.0, change=4.75, price=99.75)], losers=[]
    )

    (gainer,) = broker.get_movers(1).gainers

    assert gainer.change_pct == pytest.approx(0.05)
    assert gainer.change == Decimal("4.75")


def test_daily_bars_use_sip_history_ending_before_today(
    broker: AlpacaBroker, stock_data: MagicMock
) -> None:
    stock_data.get_stock_bars.return_value = BarSet(
        {
            "XYZ": [
                {
                    "t": "2026-11-02T05:00:00Z",
                    "o": 1,
                    "h": 2,
                    "l": 0.5,
                    "c": 1.5,
                    "v": 900,
                    "n": 9,
                    "vw": 1.4,
                },
                {
                    "t": "2026-10-30T04:00:00Z",
                    "o": 1,
                    "h": 2,
                    "l": 0.5,
                    "c": 1.25,
                    "v": 800,
                    "n": 8,
                    "vw": 1.2,
                },
            ]
        }
    )

    bars = broker.get_daily_bars("XYZ", 2)

    request = stock_data.get_stock_bars.call_args.args[0]
    today_new_york = datetime.now(_NEW_YORK).replace(hour=0, minute=0, second=0, microsecond=0)
    assert request.feed == DataFeed.SIP
    assert (request.limit, request.sort.value) == (2, "desc")
    # The SDK stores the end as naive UTC.
    assert request.end.replace(tzinfo=UTC) < today_new_york
    assert request.end.replace(tzinfo=UTC) <= datetime.now(UTC) - timedelta(minutes=15)
    assert [bar.close for bar in bars] == [Decimal("1.25"), Decimal("1.5")]
    assert bars[-1].volume == 900


def test_bars_reject_unknown_timeframe(broker: AlpacaBroker, stock_data: MagicMock) -> None:
    with pytest.raises(BrokerError, match="timeframe"):
        broker.get_bars("XYZ", "90Min", 10)

    stock_data.get_stock_bars.assert_not_called()


def test_calendar_session_times_are_new_york_times(
    broker: AlpacaBroker, trading: MagicMock
) -> None:
    trading.get_calendar.return_value = [Calendar(date="2026-11-27", open="09:30", close="13:00")]

    (day,) = broker.get_calendar(date(2026, 11, 27), date(2026, 11, 27))

    assert day.open == datetime(2026, 11, 27, 9, 30, tzinfo=_NEW_YORK)
    assert day.close.astimezone(UTC) == datetime(2026, 11, 27, 18, 0, tzinfo=UTC)
