"""Exit tools through the MCP server: `close_position`, `sell`, `tighten_stop` and
`cancel_entry_order` on brackets this server placed.

Each starts from a real `propose_buy`, so the bracket legs that reserve the shares are
the ones the server created. Rejections must leave every order as it was, and no halt
or missing state may stop an exit.
"""

from decimal import Decimal
from typing import Any

import pytest
from fastmcp import FastMCP

from trader_broker.brokers.fake import FakeBroker
from trader_broker.models import BracketOrderRequest

from .conftest import (
    REFUSED,
    ServerFactory,
    bracket_of,
    buy_arguments,
    call_tool,
    orders_for,
    place_filled_buy,
)


def open_stops(broker: FakeBroker, symbol: str = "XYZ") -> list[tuple[int, Decimal | None, str]]:
    return [
        (order.qty, order.stop_price, order.time_in_force)
        for order in orders_for(broker, symbol, "open")
        if order.side == "sell" and order.type == "stop"
    ]


def held(broker: FakeBroker, symbol: str = "XYZ") -> int:
    return next((p.qty for p in broker.get_positions() if p.symbol == symbol), 0)


def test_close_position_cancels_the_legs_and_sells_every_share(
    server: FastMCP, fake_broker: FakeBroker
) -> None:
    placed = place_filled_buy(server, fake_broker)

    result = call_tool(server, "close_position", symbol="xyz")

    assert result["status"] == "done"
    [sale] = result["orders"]
    assert (sale["type"], sale["time_in_force"], sale["qty"]) == ("market", "day", 200)
    assert held(fake_broker) == 0
    assert orders_for(fake_broker, "XYZ", "open") == []
    _, take_profit, stop = bracket_of(fake_broker, placed["order"]["order_id"])
    assert (take_profit.status, stop.status) == ("canceled", "canceled")


def test_partial_sell_keeps_the_rest_under_the_previous_raised_stop(
    server: FastMCP, fake_broker: FakeBroker
) -> None:
    place_filled_buy(server, fake_broker)
    assert call_tool(server, "tighten_stop", symbol="XYZ", new_stop=97)["status"] == "done"

    result = call_tool(server, "sell", symbol="XYZ", qty=50)

    assert result["status"] == "done"
    assert held(fake_broker) == 150
    assert open_stops(fake_broker) == [(150, Decimal(97), "gtc")]
    [protection] = result["protection_repairs"]
    assert (protection["action"], protection["uncovered_qty"]) == ("stop_placed", 150)


@pytest.mark.parametrize(
    ("tool", "arguments", "reason"),
    [
        pytest.param("sell", {"symbol": "XYZ", "qty": 201}, "exceeds_position", id="one-too-many"),
        pytest.param("sell", {"symbol": "XYZ", "qty": 10**12}, "exceeds_position", id="absurd-qty"),
        pytest.param("sell", {"symbol": "ABC", "qty": 1}, "exceeds_position", id="not-held"),
        pytest.param("sell", {"symbol": "XYZ", "qty": 0}, "invalid_qty", id="zero"),
        pytest.param("sell", {"symbol": "XYZ", "qty": -5}, "invalid_qty", id="negative"),
        pytest.param("close_position", {"symbol": "ABC"}, "no_position", id="close-not-held"),
    ],
)
def test_invalid_sells_are_rejected_and_touch_nothing(
    server: FastMCP, fake_broker: FakeBroker, tool: str, arguments: dict[str, Any], reason: str
) -> None:
    placed = place_filled_buy(server, fake_broker)
    before = orders_for(fake_broker, "XYZ")

    result = call_tool(server, tool, **arguments)

    assert (result["status"], result["reason"]) == ("rejected", reason)
    assert orders_for(fake_broker, "XYZ") == before
    assert orders_for(fake_broker, "ABC") == []
    assert all(position.qty > 0 for position in fake_broker.get_positions())
    _, take_profit, stop = bracket_of(fake_broker, placed["order"]["order_id"])
    assert take_profit.is_open and stop.is_open


def test_tighten_stop_only_raises_the_stop_below_the_bid(
    server: FastMCP, fake_broker: FakeBroker
) -> None:
    place_filled_buy(server, fake_broker, stop_price=90)

    raised = call_tool(server, "tighten_stop", symbol="XYZ", new_stop=93)
    rejected = {
        new_stop: call_tool(server, "tighten_stop", symbol="XYZ", new_stop=new_stop)["reason"]
        for new_stop in ("88", "93", "92.999", "99.98", "105")
    }

    assert raised["status"] == "done"
    assert rejected == {
        "88": "stop_not_raised",
        "93": "stop_not_raised",
        "92.999": "stop_not_raised",
        "99.98": "stop_at_or_above_bid",
        "105": "stop_at_or_above_bid",
    }
    with pytest.raises(REFUSED):
        call_tool(server, "tighten_stop", symbol="XYZ", new_stop="abc")
    assert open_stops(fake_broker) == [(200, Decimal(93), "gtc")]
    [entry] = call_tool(server, "get_journal")["entries"]
    assert Decimal(entry["stop_price"]) == 93


@pytest.mark.parametrize("new_stop", ["1e400", "1e30", "NaN", "Infinity", "-Infinity", "-1", "0"])
def test_out_of_range_stop_is_an_invalid_request_that_touches_nothing(
    server: FastMCP, fake_broker: FakeBroker, new_stop: str
) -> None:
    place_filled_buy(server, fake_broker)
    before = orders_for(fake_broker, "XYZ")

    result = call_tool(server, "tighten_stop", symbol="XYZ", new_stop=new_stop)

    assert (result["status"], result["reason"]) == ("error", "invalid_request")
    assert result["message"].startswith("new_stop: ")
    assert orders_for(fake_broker, "XYZ") == before


def place_filled_buy_and_pending_add_on(server: FastMCP, fake_broker: FakeBroker) -> str:
    """150 XYZ bought with a stop at 80, then an unfilled 50-share add-on bracket with a
    stop at 95; returns the add-on's entry id."""
    place_filled_buy(server, fake_broker, stop_price=80)
    add_on = call_tool(server, "propose_buy", **buy_arguments())["order"]
    assert (held(fake_broker), add_on["qty"]) == (150, 50)
    return str(add_on["order_id"])


def test_partial_sell_leaves_a_pending_add_on_bracket_alone(
    server: FastMCP, fake_broker: FakeBroker
) -> None:
    add_on_id = place_filled_buy_and_pending_add_on(server, fake_broker)
    add_on_before = bracket_of(fake_broker, add_on_id)

    result = call_tool(server, "sell", symbol="XYZ", qty=50)

    assert result["status"] == "done"
    assert bracket_of(fake_broker, add_on_id) == add_on_before
    [protection] = result["protection_repairs"]
    assert (protection["uncovered_qty"], Decimal(protection["stop_price"])) == (100, 80)
    fake_broker.fill_order(add_on_id)
    assert sorted(open_stops(fake_broker)) == [(50, Decimal(95), "gtc"), (100, Decimal(80), "gtc")]


def test_close_position_also_cancels_a_pending_add_on_bracket(
    server: FastMCP, fake_broker: FakeBroker
) -> None:
    add_on_id = place_filled_buy_and_pending_add_on(server, fake_broker)

    result = call_tool(server, "close_position", symbol="XYZ")

    assert result["status"] == "done"
    assert held(fake_broker) == 0
    assert orders_for(fake_broker, "XYZ", "open") == []
    assert {order.status for order in bracket_of(fake_broker, add_on_id)} == {"canceled"}


def test_tighten_stop_raises_only_the_stops_of_shares_held(
    server: FastMCP, fake_broker: FakeBroker
) -> None:
    add_on_id = place_filled_buy_and_pending_add_on(server, fake_broker)

    result = call_tool(server, "tighten_stop", symbol="XYZ", new_stop=90)

    assert result["status"] == "done", result
    assert sorted(open_stops(fake_broker)) == [(50, Decimal(95), "gtc"), (150, Decimal(90), "gtc")]
    assert bracket_of(fake_broker, add_on_id)[2].stop_price == 95
    journal_stops = {
        entry["qty"]: Decimal(entry["stop_price"])
        for entry in call_tool(server, "get_journal")["entries"]
    }
    assert journal_stops == {150: Decimal(90), 50: Decimal(95)}


def test_cancel_entry_order_cancels_only_this_servers_unfilled_entries(
    server: FastMCP, fake_broker: FakeBroker
) -> None:
    partly_filled = call_tool(server, "propose_buy", **buy_arguments(stop_price=80))["order"]
    fake_broker.fill_order(partly_filled["order_id"], qty=10)
    unfilled = call_tool(server, "propose_buy", **buy_arguments())["order"]
    foreign = fake_broker.submit_bracket_buy(
        BracketOrderRequest(
            symbol="ABC",
            qty=5,
            limit_price=Decimal(50),
            stop_price=Decimal(45),
            take_profit_price=Decimal(60),
            client_order_id="manual-web-order",
        )
    )
    _, take_profit_leg, stop_leg = bracket_of(fake_broker, unfilled["order_id"])

    refused = {
        name: call_tool(server, "cancel_entry_order", order_id=order_id)["reason"]
        for name, order_id in {
            "stop leg": stop_leg.id,
            "take-profit leg": take_profit_leg.id,
            "partly filled": partly_filled["order_id"],
            "foreign": foreign.id,
            "unknown": "order-does-not-exist",
        }.items()
    }
    open_before = {order.id for order in fake_broker.get_orders("open", limit=1000)}
    cancelled = call_tool(server, "cancel_entry_order", order_id=unfilled["order_id"])

    assert set(refused.values()) == {"not_cancellable"}
    assert cancelled["status"] == "done"
    entry, take_profit_leg, stop_leg = bracket_of(fake_broker, unfilled["order_id"])
    assert {entry.status, take_profit_leg.status, stop_leg.status} == {"canceled"}
    still_open = {order.id for order in fake_broker.get_orders("open", limit=1000)}
    assert still_open == open_before - {entry.id, take_profit_leg.id, stop_leg.id}
    assert {partly_filled["order_id"], foreign.id} <= still_open


def _assert_every_exit_works(server: FastMCP, fake_broker: FakeBroker, entry_id: str) -> None:
    assert call_tool(server, "tighten_stop", symbol="XYZ", new_stop=96)["status"] == "done"
    assert call_tool(server, "sell", symbol="XYZ", qty=50)["status"] == "done"
    assert call_tool(server, "cancel_entry_order", order_id=entry_id)["status"] == "done"
    assert call_tool(server, "close_position", symbol="XYZ")["status"] == "done"
    assert held(fake_broker) == 0
    assert orders_for(fake_broker, "XYZ", "open") == []


def test_exits_work_while_the_kill_switch_and_daily_halt_are_set(
    server: FastMCP, fake_broker: FakeBroker
) -> None:
    place_filled_buy(server, fake_broker, stop_price=80)  # 150 shares, $85k cash
    unfilled = call_tool(server, "propose_buy", **buy_arguments())["order"]
    fake_broker.set_account(cash=Decimal(60_000), last_equity=Decimal(100_000))  # equity 75k

    status = call_tool(server, "get_risk_status")
    assert (status["kill_switch"], status["daily_halt"]) == (True, True)
    assert call_tool(server, "propose_buy", **buy_arguments())["status"] == "blocked"

    _assert_every_exit_works(server, fake_broker, unfilled["order_id"])


def test_exits_work_without_a_state_database(
    make_server: ServerFactory, fake_broker: FakeBroker
) -> None:
    fake_broker.set_account(cash=Decimal(85_000))
    fake_broker.set_position("XYZ", 150, Decimal(100))
    entry = fake_broker.submit_bracket_buy(
        BracketOrderRequest(
            symbol="XYZ",
            qty=50,
            limit_price=Decimal(100),
            stop_price=Decimal(95),
            take_profit_price=Decimal(120),
            client_order_id="tb-placed-before-the-state-was-lost",
        )
    )
    server = make_server(state=None)
    call_tool(server, "get_risk_status")  # protects the 150 shares with a stop at 92

    _assert_every_exit_works(server, fake_broker, entry.id)
