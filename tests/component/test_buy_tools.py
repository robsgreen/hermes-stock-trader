"""`propose_buy` through the MCP server: bracket placement, server-side sizing, the
symbol cap across buys, every entry rule, and lost submit replies.

Drives the real server, service, risk gate, review decision, state database and journal
over `FakeBroker`, so these catch wiring breaks the per-module tests cannot.
"""

import asyncio
from decimal import Decimal
from typing import Any

import pytest
from fastmcp import FastMCP

from trader_broker.brokers.base import UnknownResultError
from trader_broker.brokers.fake import FakeBroker
from trader_broker.models import Account, Asset
from trader_broker.state import StateStore

from .conftest import (
    REFUSED,
    MarketTime,
    ToolResult,
    at,
    bracket_of,
    buy_arguments,
    call_tool,
    dec,
    orders_for,
)


def propose(server: FastMCP, **overrides: Any) -> ToolResult:
    return call_tool(server, "propose_buy", **buy_arguments(**overrides))


def rule_ids(result: ToolResult) -> list[str]:
    return [reason.split(":", 1)[0] for reason in result["reasons"]]


def test_approved_buy_places_one_gtc_bracket_and_reports_it(
    server: FastMCP, fake_broker: FakeBroker
) -> None:
    result = propose(server, symbol="  xyz ")

    assert result["status"] == "placed"
    order = result["order"]
    assert order["symbol"] == "XYZ"
    assert (order["qty"], dec(order["limit_price"])) == (200, Decimal(100))
    assert (dec(order["stop_price"]), dec(order["target_price"])) == (Decimal(95), Decimal(120))
    assert order["client_order_id"].startswith("tb-")
    assert [(review["reviewer"], review["verdict"]) for review in result["reviews"]] == [
        ("openai", "approve"),
        ("xai", "approve"),
    ]
    assert all(review["reasons"] for review in result["reviews"])

    [entry] = [o for o in orders_for(fake_broker, "XYZ") if o.parent_id is None]
    assert (entry.id, entry.client_order_id) == (order["order_id"], order["client_order_id"])
    _, take_profit, stop = bracket_of(fake_broker, entry.id)
    assert (entry.side, entry.type, entry.order_class) == ("buy", "limit", "bracket")
    assert {entry.time_in_force, take_profit.time_in_force, stop.time_in_force} == {"gtc"}
    assert (entry.qty, take_profit.qty, stop.qty) == (200, 200, 200)
    assert (take_profit.limit_price, stop.stop_price) == (Decimal(120), Decimal(95))


def test_propose_buy_offers_no_share_count_and_refuses_one(server: FastMCP) -> None:
    tools = {tool.name: tool for tool in asyncio.run(server.list_tools())}
    parameters = set(tools["propose_buy"].parameters["properties"])

    assert not {"qty", "quantity", "shares", "size", "notional"} & parameters
    with pytest.raises(REFUSED):
        propose(server, qty=1000)


@pytest.mark.parametrize(
    ("overrides", "cash_held_in_abc", "qty"),
    [
        pytest.param({"limit_price": 100, "stop_price": 95}, 0, 200, id="cap-bound"),
        pytest.param(
            {"limit_price": 50, "stop_price": 40, "target_price": 60}, 0, 300, id="risk-bound"
        ),
        pytest.param({"limit_price": 100, "stop_price": 95}, 90_000, 100, id="cash-bound"),
    ],
)
def test_placed_qty_is_the_code_sized_qty(
    server: FastMCP,
    fake_broker: FakeBroker,
    overrides: dict[str, Any],
    cash_held_in_abc: int,
    qty: int,
) -> None:
    if cash_held_in_abc:
        # $90k of the $100k equity sits in another stock, leaving $10k cash.
        fake_broker.set_account(cash=Decimal(100_000 - cash_held_in_abc))
        fake_broker.set_position("ABC", cash_held_in_abc // 100, Decimal(100), Decimal(100))

    result = propose(server, **overrides)

    assert result["status"] == "placed", result
    assert result["order"]["qty"] == qty
    [entry] = [o for o in orders_for(fake_broker, "XYZ", "open") if o.parent_id is None]
    assert entry.qty == qty


def test_existing_position_limits_the_add_to_the_symbol_cap(
    server: FastMCP, fake_broker: FakeBroker
) -> None:
    fake_broker.set_account(cash=Decimal(85_000))
    fake_broker.set_position("XYZ", 150, Decimal(100))

    result = propose(server)

    assert result["order"]["qty"] == 50


def test_open_entry_from_an_earlier_buy_counts_toward_the_symbol_cap(server: FastMCP) -> None:
    first = propose(server, stop_price=80)  # risk-bound: $3k / $20 = 150 shares
    second = propose(server)
    third = propose(server)

    assert (first["order"]["qty"], second["order"]["qty"]) == (150, 50)
    assert third["status"] == "blocked"
    assert rule_ids(third) == ["qty_zero"]


def test_racing_buys_never_exceed_the_symbol_cap_together(
    server: FastMCP, fake_broker: FakeBroker
) -> None:
    async def race() -> list[Any]:
        return list(
            await asyncio.gather(
                *(server.call_tool("propose_buy", buy_arguments(stop_price=80)) for _ in range(3))
            )
        )

    asyncio.run(race())

    entries = [o for o in orders_for(fake_broker, "XYZ", "open") if o.parent_id is None]
    assert sum(entry.qty * entry.limit_price for entry in entries if entry.limit_price) <= 20_000
    assert sorted(entry.qty for entry in entries) == [50, 150]


def _thin_volume(fake_broker: FakeBroker) -> None:
    bars = fake_broker.get_daily_bars("XYZ", 260)
    fake_broker.set_daily_bars("XYZ", [bar.model_copy(update={"volume": 50_000}) for bar in bars])


def _halted_asset(fake_broker: FakeBroker) -> None:
    fake_broker.set_asset(
        Asset(
            symbol="XYZ",
            asset_class="us_equity",
            exchange="NASDAQ",
            status="active",
            tradable=False,
        )
    )


@pytest.mark.parametrize(
    ("rule", "setup", "overrides"),
    [
        pytest.param(
            "min_price",
            None,
            {"limit_price": "4.99", "stop_price": "4.80", "target_price": 6},
            id="min_price",
        ),
        pytest.param("min_adv", _thin_volume, {}, id="min_adv"),
        pytest.param("not_tradable", _halted_asset, {}, id="not_tradable"),
        pytest.param("stop_invalid", None, {"stop_price": 100}, id="stop-at-entry"),
        pytest.param("stop_invalid", None, {"stop_price": 70}, id="stop-30pct-away"),
        pytest.param("target_invalid", None, {"target_price": "100.01"}, id="target_invalid"),
        pytest.param(
            "qty_zero",
            lambda broker: broker.set_account(cash=Decimal(50)),
            {},
            id="qty_zero",
        ),
    ],
)
def test_each_entry_rule_blocks_with_its_id_and_is_recorded(
    server: FastMCP,
    fake_broker: FakeBroker,
    rule: str,
    setup: Any,
    overrides: dict[str, Any],
) -> None:
    if setup:
        setup(fake_broker)

    result = propose(server, **overrides)

    assert result["status"] == "blocked"
    assert result["order"] is None
    assert rule in rule_ids(result), result["reasons"]
    assert [o for o in orders_for(fake_broker, "XYZ") if o.side == "buy"] == []
    [block] = call_tool(server, "get_risk_status")["recent_blocks"]
    assert (block["tool"], block["symbol"], block["reasons"]) == (
        "propose_buy",
        "XYZ",
        result["reasons"],
    )


@pytest.mark.parametrize(
    ("moment", "rule"),
    [
        pytest.param(at(22), "market_closed", id="after-the-close"),
        pytest.param(at(20, 40), "too_late", id="20-minutes-before-close"),
    ],
)
def test_session_rules_block_by_the_broker_clock(
    server: FastMCP, market_time: MarketTime, moment: Any, rule: str
) -> None:
    market_time.set(moment)

    result = propose(server)

    assert result["status"] == "blocked"
    assert rule in rule_ids(result)


def test_thirty_one_minutes_before_close_is_still_allowed(
    server: FastMCP, market_time: MarketTime
) -> None:
    market_time.set(at(20, 29))

    assert propose(server)["status"] == "placed"


def test_stale_account_read_that_would_lever_the_account_is_blocked(
    server: FastMCP, fake_broker: FakeBroker, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The account snapshot predates a $90k manual buy that the positions already show.
    fake_broker.set_account(cash=Decimal(10_000))
    fake_broker.set_position("ABC", 900, Decimal(100), Decimal(100))
    stale = Account(
        equity=Decimal(100_000),
        last_equity=Decimal(100_000),
        cash=Decimal(100_000),
        buying_power=Decimal(100_000),
    )
    monkeypatch.setattr(fake_broker, "get_account", lambda: stale)

    result = propose(server)

    assert result["status"] == "blocked"
    assert "leverage" in rule_ids(result)


def test_a_proposal_breaking_several_rules_lists_every_one(
    server: FastMCP, fake_broker: FakeBroker, market_time: MarketTime
) -> None:
    _thin_volume(fake_broker)
    _halted_asset(fake_broker)
    market_time.set(at(20, 40))

    result = propose(server)

    assert {"min_adv", "not_tradable", "too_late"} <= set(rule_ids(result))


def test_a_proposal_with_an_invalid_stop_and_target_still_lists_the_entry_rules(
    server: FastMCP, fake_broker: FakeBroker, market_time: MarketTime
) -> None:
    _thin_volume(fake_broker)
    market_time.set(at(22))

    result = propose(server, stop_price=100, target_price=100)

    assert result["status"] == "blocked"
    assert rule_ids(result) == [
        "stop_invalid",
        "target_invalid",
        "stop_invalid",  # also judged against the bid, 99.98
        "min_adv",
        "market_closed",
        "too_late",
    ]
    [block] = call_tool(server, "get_risk_status")["recent_blocks"]
    assert block["reasons"] == result["reasons"]


@pytest.mark.parametrize(
    "overrides",
    [
        pytest.param({"stop_price": -5}, id="negative-stop"),
        pytest.param({"limit_price": 0}, id="zero-limit"),
        pytest.param({"stop_price": "1e400"}, id="stop-beyond-any-price"),
        pytest.param({"limit_price": "1e400"}, id="limit-beyond-any-price"),
        pytest.param({"target_price": "1e30"}, id="target-beyond-any-price"),
        pytest.param({"stop_price": "NaN"}, id="nan-stop"),
        pytest.param({"target_price": "Infinity"}, id="infinite-target"),
        pytest.param({"limit_price": "-Infinity"}, id="negative-infinite-limit"),
        pytest.param({"horizon_days": 0}, id="zero-horizon"),
        pytest.param({"thesis": ""}, id="empty-thesis"),
    ],
)
def test_malformed_proposal_is_an_invalid_request_that_never_reaches_the_broker(
    server: FastMCP, fake_broker: FakeBroker, overrides: dict[str, Any]
) -> None:
    result = propose(server, **overrides)

    assert (result["status"], result["reason"]) == ("error", "invalid_request"), result
    assert orders_for(fake_broker, "XYZ") == []


def test_submit_lost_after_the_broker_accepted_it_counts_as_placed_once(
    server: FastMCP, fake_broker: FakeBroker, state_store: StateStore
) -> None:
    fake_broker.accept_then_time_out("submit_bracket_buy")

    result = propose(server)

    assert result["status"] == "placed"
    entries = [o for o in orders_for(fake_broker, "XYZ") if o.side == "buy"]
    assert [entry.id for entry in entries] == [result["order"]["order_id"]]
    stored = state_store.order(result["order"]["client_order_id"])
    assert stored is not None and stored.broker_order_id == entries[0].id
    [journal_entry] = call_tool(server, "get_journal")["entries"]
    assert journal_entry["client_order_id"] == result["order"]["client_order_id"]


def test_submit_lost_without_an_order_is_unknown_and_never_resubmitted(
    server: FastMCP, fake_broker: FakeBroker, monkeypatch: pytest.MonkeyPatch
) -> None:
    submitted: list[str] = []
    real_submit = fake_broker.submit_bracket_buy

    def counted(request: Any) -> Any:
        submitted.append(request.client_order_id)
        return real_submit(request)

    monkeypatch.setattr(fake_broker, "submit_bracket_buy", counted)
    fake_broker.fail_next("submit_bracket_buy", UnknownResultError("read timed out"))

    result = propose(server)

    assert result["status"] == "unknown"
    assert "not resubmitted" in result["reasons"][0]
    assert len(submitted) == 1
    assert orders_for(fake_broker, "XYZ") == []
    assert call_tool(server, "get_journal")["entries"] == []
    again = propose(server)
    assert (again["status"], rule_ids(again)) == ("blocked", ["pending_unknown"])
    assert len(submitted) == 1
