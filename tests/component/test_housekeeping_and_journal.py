"""Housekeeping and the journal, as tool calls trigger them through the MCP server.

Stale entries are cancelled, unprotected positions are stopped or sold, reconciliation
counts each integrity violation once, and a trade bought through `propose_buy` is
closed, settled against SPY and given a lesson.
"""

import logging
import sqlite3
from collections.abc import Callable
from datetime import datetime
from decimal import Decimal

import pytest
from fastmcp import FastMCP

from trader_broker.brokers.fake import FakeBroker
from trader_broker.models import BracketOrderRequest, SizedOrder
from trader_broker.state import StateStore

from .conftest import (
    MarketTime,
    at,
    bracket_of,
    buy_arguments,
    call_tool,
    daily_close,
    dec,
    orders_for,
    place_filled_buy,
)


def stops(broker: FakeBroker, symbol: str) -> list[tuple[int, Decimal | None, str]]:
    return [
        (order.qty, order.stop_price, order.time_in_force)
        for order in orders_for(broker, symbol, "open")
        if order.side == "sell" and order.type == "stop"
    ]


def any_write_tool(server: FastMCP) -> None:
    call_tool(server, "close_position", symbol="NOTHELD")


def risk_status(server: FastMCP) -> None:
    call_tool(server, "get_risk_status")


@pytest.mark.parametrize(
    "trigger", [any_write_tool, risk_status], ids=["write-tool", "risk-status"]
)
def test_unfilled_entry_is_cancelled_once_older_than_thirty_minutes(
    server: FastMCP,
    fake_broker: FakeBroker,
    market_time: MarketTime,
    trigger: Callable[[FastMCP], None],
) -> None:
    entry_id = call_tool(server, "propose_buy", **buy_arguments())["order"]["order_id"]

    market_time.advance(minutes=29)
    trigger(server)
    assert bracket_of(fake_broker, entry_id)[0].is_open

    market_time.advance(minutes=2)
    trigger(server)
    entry, take_profit, stop = bracket_of(fake_broker, entry_id)
    assert {entry.status, take_profit.status, stop.status} == {"canceled"}
    assert call_tool(server, "get_risk_status")["open_entry_orders"] == []


def test_manual_position_gets_one_stop_eight_percent_below_its_entry(
    server: FastMCP, fake_broker: FakeBroker
) -> None:
    fake_broker.set_account(cash=Decimal(95_000))
    fake_broker.set_position("ABC", 100, Decimal(50), Decimal(55))

    first = call_tool(server, "get_risk_status")
    second = call_tool(server, "get_risk_status")

    [unprotected] = first["protection_repairs"]
    assert (unprotected["symbol"], unprotected["uncovered_qty"]) == ("ABC", 100)
    assert (dec(unprotected["stop_price"]), unprotected["action"]) == (Decimal(46), "stop_placed")
    assert second["protection_repairs"] == []
    assert stops(fake_broker, "ABC") == [(100, Decimal(46), "gtc")]


def test_bought_position_whose_legs_were_cancelled_gets_its_journal_stop(
    server: FastMCP, fake_broker: FakeBroker
) -> None:
    placed = place_filled_buy(server, fake_broker, stop_price=93)
    _, take_profit, stop = bracket_of(fake_broker, placed["order"]["order_id"])
    fake_broker.cancel_order(stop.id)
    fake_broker.cancel_order(take_profit.id)

    status = call_tool(server, "get_risk_status")

    assert [(u["symbol"], u["action"]) for u in status["protection_repairs"]] == [
        ("XYZ", "stop_placed")
    ]
    assert stops(fake_broker, "XYZ") == [(200, Decimal(93), "gtc")]


def test_bought_position_whose_stop_leg_alone_was_cancelled_is_protected_again(
    server: FastMCP, fake_broker: FakeBroker
) -> None:
    # The open take-profit leg reserves every share, so it must go before a stop can.
    placed = place_filled_buy(server, fake_broker, stop_price=93)
    entry_id = placed["order"]["order_id"]
    fake_broker.cancel_order(bracket_of(fake_broker, entry_id)[2].id)

    first = call_tool(server, "get_risk_status")
    second = call_tool(server, "get_risk_status")

    assert [(u["action"], u["uncovered_qty"]) for u in first["protection_repairs"]] == [
        ("stop_placed", 200)
    ]
    assert second["protection_repairs"] == []
    assert stops(fake_broker, "XYZ") == [(200, Decimal(93), "gtc")]
    assert bracket_of(fake_broker, entry_id)[1].status == "canceled"


def test_unprotected_shares_below_their_stop_are_sold_and_logged(
    server: FastMCP, fake_broker: FakeBroker, caplog: pytest.LogCaptureFixture
) -> None:
    fake_broker.set_account(cash=Decimal(95_000))
    fake_broker.set_position("ABC", 100, Decimal(50), Decimal(45))

    with caplog.at_level(logging.WARNING, logger="trader_broker.service"):
        status = call_tool(server, "get_risk_status")

    [unprotected] = status["protection_repairs"]
    assert (unprotected["action"], dec(unprotected["stop_price"])) == ("sold", Decimal(46))
    assert [p.symbol for p in fake_broker.get_positions()] == []
    [sale] = orders_for(fake_broker, "ABC")
    assert (sale.type, sale.status, sale.qty) == ("market", "filled", 100)
    assert any("sold 100 unprotected ABC" in record.getMessage() for record in caplog.records)


def _violation_kinds(state: StateStore) -> list[str]:
    with sqlite3.connect(state.path) as connection:
        return [row[0] for row in connection.execute("SELECT kind FROM integrity_violations")]


def _foreign_bracket(broker: FakeBroker, state: StateStore, now: datetime) -> None:
    broker.submit_bracket_buy(
        BracketOrderRequest(
            symbol="ABC",
            qty=10,
            limit_price=Decimal(50),
            stop_price=Decimal(45),
            take_profit_price=Decimal(60),
            client_order_id="placed-in-the-alpaca-dashboard",
        )
    )


def _foreign_position(broker: FakeBroker, state: StateStore, now: datetime) -> None:
    broker.set_account(cash=Decimal(99_800))
    broker.set_position("DEF", 10, Decimal(20), Decimal(20))


def _entry_sized_over_the_cap(broker: FakeBroker, state: StateStore, now: datetime) -> None:
    client_order_id = "tb-oversized-by-an-older-version"
    state.record_order_intent(
        client_order_id,
        "XYZ",
        "buy",
        "entry",
        now,
        sizing=SizedOrder(
            symbol="XYZ",
            qty=300,
            entry_price=Decimal(100),
            stop_price=Decimal(95),
            target_price=Decimal(120),
            notional=Decimal(30_000),
            pct_equity=0.30,
            risk_usd=Decimal(1_500),
            pct_equity_risk=0.015,
            bound_by="risk_size",
        ),
    )
    entry = broker.submit_bracket_buy(
        BracketOrderRequest(
            symbol="XYZ",
            qty=300,
            limit_price=Decimal(100),
            stop_price=Decimal(95),
            take_profit_price=Decimal(120),
            client_order_id=client_order_id,
        )
    )
    state.update_order(client_order_id, entry.status, entry.id)


def _negative_cash(broker: FakeBroker, state: StateStore, now: datetime) -> None:
    broker.set_account(cash=Decimal(-1_000), last_equity=Decimal(-1_000))


@pytest.mark.parametrize(
    ("setup", "kind"),
    [
        pytest.param(_foreign_bracket, "foreign_order", id="foreign-order"),
        pytest.param(_foreign_position, "foreign_position", id="foreign-position"),
        pytest.param(_entry_sized_over_the_cap, "entry_over_cap", id="entry-over-cap"),
        pytest.param(_negative_cash, "negative_cash", id="negative-cash"),
    ],
)
def test_each_integrity_violation_is_recorded_once(
    server: FastMCP,
    fake_broker: FakeBroker,
    state_store: StateStore,
    now: datetime,
    setup: Callable[[FakeBroker, StateStore, datetime], None],
    kind: str,
) -> None:
    setup(fake_broker, state_store, now)

    for _ in range(3):
        call_tool(server, "get_risk_status")
    call_tool(server, "sell", symbol="NOTHELD", qty=1)

    assert _violation_kinds(state_store) == [kind]


def test_price_drift_past_the_symbol_cap_is_not_a_violation(
    server: FastMCP, fake_broker: FakeBroker, state_store: StateStore
) -> None:
    place_filled_buy(server, fake_broker)
    fake_broker.set_price("XYZ", Decimal(130))

    status = call_tool(server, "get_risk_status")
    call_tool(server, "get_risk_status")

    assert status["symbol_pct"]["XYZ"] > 0.2
    assert state_store.integrity_violation_count() == 0


def test_a_trade_is_journaled_settled_against_spy_and_given_a_lesson(
    server: FastMCP, fake_broker: FakeBroker, market_time: MarketTime
) -> None:
    placed = place_filled_buy(server, fake_broker)
    spy_history = fake_broker.get_daily_bars("SPY", 260)

    market_time.set(at(15, 30, day=4))
    fake_broker.set_quote("XYZ", bid=Decimal(110), ask=Decimal("110.02"))
    assert call_tool(server, "close_position", symbol="XYZ")["status"] == "done"
    call_tool(server, "get_risk_status")  # housekeeping closes entries whose symbol is gone
    [closed] = call_tool(server, "get_journal", status="closed")["entries"]
    assert (dec(closed["exit_price"]), closed["trade_return_pct"]) == (Decimal(110), 0.1)
    assert call_tool(server, "get_journal", status="pending_lessons")["entries"] == []

    market_time.set(at(15, 30, day=5))
    fake_broker.set_daily_bars("SPY", [*spy_history, daily_close(3, 100), daily_close(4, 102)])
    call_tool(server, "get_risk_status")

    [pending] = call_tool(server, "get_journal", status="pending_lessons")["entries"]
    assert pending["client_order_id"] == placed["order"]["client_order_id"]
    assert pending["spy_return_pct"] == pytest.approx(0.02)
    assert pending["alpha_pct"] == pytest.approx(0.08)
    assert pending["lesson_pending"] is True

    noted = call_tool(server, "journal_note", entry_id=pending["id"], note="Let winners run.")

    assert (noted["lesson"], noted["lesson_pending"]) == ("Let winners run.", False)
    assert call_tool(server, "get_journal", status="pending_lessons")["entries"] == []
    assert call_tool(server, "journal_note", entry_id=pending["id"] + 1, note="x")["reason"] == (
        "unknown_entry"
    )


def test_journal_entry_reads_closed_as_soon_as_the_position_is_closed(
    server: FastMCP, fake_broker: FakeBroker, market_time: MarketTime
) -> None:
    place_filled_buy(server, fake_broker)
    market_time.set(at(15, 30, day=4))
    fake_broker.set_quote("XYZ", bid=Decimal(110), ask=Decimal("110.02"))

    assert call_tool(server, "close_position", symbol="XYZ")["status"] == "done"

    assert call_tool(server, "get_journal", status="open")["entries"] == []
    [closed] = call_tool(server, "get_journal", status="closed")["entries"]
    assert dec(closed["exit_price"]) == Decimal(110)
