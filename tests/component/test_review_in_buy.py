"""The cross-model review inside `propose_buy`, driven through the MCP server.

Covers the verdict rule end to end, reviewers that fail (including the real
OpenAI-compatible reviewer against a scripted HTTP API), reviewer changes that may only
make the order safer, and what the server shows the reviewers and stores about them.
"""

import json
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import httpx
import pytest
from openai import OpenAI

from trader_broker.brokers.fake import FakeBroker
from trader_broker.models import NewsItem, ReviewVerdict
from trader_broker.review import OpenAICompatReviewer, Reviewer
from trader_broker.state import StateStore

from .conftest import (
    APPROVE,
    ServerFactory,
    bracket_of,
    buy_arguments,
    call_tool,
    dec,
    orders_for,
    place_filled_buy,
    verdict,
)

EPOCH = datetime(2000, 1, 1, tzinfo=UTC)


@pytest.mark.parametrize(
    ("first", "second", "status"),
    [
        pytest.param(verdict("approve"), verdict("approve"), "placed", id="approve-approve"),
        pytest.param(
            verdict("approve"), verdict("reject", "low"), "placed", id="approve-low-reject"
        ),
        pytest.param(
            verdict("approve"), verdict("reject", "high"), "blocked", id="approve-high-reject"
        ),
        pytest.param(
            verdict("reject", "low"), verdict("reject", "low"), "placed", id="two-low-rejects"
        ),
        pytest.param(
            verdict("reject", "low"), verdict("reject", "medium"), "placed", id="low-medium-rejects"
        ),
        pytest.param(
            verdict("reject", "medium"), verdict("reject", "low"), "placed", id="medium-low-rejects"
        ),
        pytest.param(
            verdict("reject", "medium"),
            verdict("reject", "medium"),
            "placed",
            id="two-medium-rejects",
        ),
        pytest.param(
            verdict("reject", "high"), verdict("modify"), "blocked", id="high-reject-modify"
        ),
        pytest.param(
            verdict("reject", "high"), verdict("reject", "high"), "blocked", id="two-high-rejects"
        ),
    ],
)
def test_verdict_combinations_decide_the_buy(
    make_server: ServerFactory,
    fake_reviewer: Callable[..., Reviewer],
    fake_broker: FakeBroker,
    state_store: StateStore,
    caplog: pytest.LogCaptureFixture,
    first: ReviewVerdict,
    second: ReviewVerdict,
    status: str,
) -> None:
    server = make_server([fake_reviewer("openai", first), fake_reviewer("xai", second)])

    with caplog.at_level("INFO", logger="trader_broker.service"):
        result = call_tool(server, "propose_buy", **buy_arguments())

    assert result["status"] == status
    assert [(r["reviewer"], r["verdict"]) for r in result["reviews"]] == [
        ("openai", first.verdict),
        ("xai", second.verdict),
    ]
    buys = [o for o in orders_for(fake_broker, "XYZ") if o.side == "buy"]
    assert len(buys) == (1 if status == "placed" else 0)
    [review] = state_store.reviews_since(EPOCH)
    assert review.decision == ("proceed" if status == "placed" else "block")
    assert [outcome.verdict for outcome in review.outcomes] == [first, second]
    if status == "placed":
        objections = [
            f"{name}: nonblocking {v.confidence}-confidence objection: {'; '.join(v.reasons)}"
            for name, v in (("openai", first), ("xai", second))
            if v.verdict == "reject"
        ]
        assert result["reasons"] == objections
        assert all(reason in caplog.text for reason in objections)
        assert review.final_order is not None and review.final_order.qty == 200
    if status == "blocked":
        assert (
            call_tool(server, "get_risk_status")["recent_blocks"][0]["reasons"]
            == (result["reasons"])
        )


def _chat_api(reply: Callable[[], httpx.Response]) -> tuple[OpenAI, list[httpx.Request]]:
    """An OpenAI client whose HTTP API answers every request with `reply()`."""
    requests: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return reply()

    client = OpenAI(
        api_key="test-key",
        base_url="https://reviewer.test/v1",
        max_retries=0,
        http_client=httpx.Client(transport=httpx.MockTransport(handle)),
    )
    return client, requests


def _completion(content: str) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "id": "chatcmpl-1",
            "object": "chat.completion",
            "created": 0,
            "model": "grok-test",
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": content},
                    "finish_reason": "stop",
                }
            ],
        },
    )


def _time_out() -> httpx.Response:
    raise httpx.ReadTimeout("read timed out")


@pytest.mark.parametrize(
    "reply",
    [
        pytest.param(lambda: _completion("Sure! I approve."), id="invalid-json"),
        pytest.param(
            lambda: _completion(
                json.dumps(
                    {
                        "verdict": "modify",
                        "confidence": "high",
                        "reasons": ["Size up."],
                        "modifications": {"size_multiplier": 1.5},
                    }
                )
            ),
            id="multiplier-above-one",
        ),
        pytest.param(_time_out, id="timeout"),
    ],
)
def test_reviewer_api_failing_twice_blocks_the_buy_naming_it(
    make_server: ServerFactory,
    fake_reviewer: Callable[..., Reviewer],
    fake_broker: FakeBroker,
    reply: Callable[[], httpx.Response],
) -> None:
    client, requests = _chat_api(reply)
    grok = OpenAICompatReviewer("xai", client, "grok-test")
    server = make_server([fake_reviewer("openai", APPROVE), grok])

    result = call_tool(server, "propose_buy", **buy_arguments())

    assert result["status"] == "blocked"
    assert len(requests) == 2
    assert [reason.split(":", 1)[0] for reason in result["reasons"]] == ["xai"]
    assert [(r["reviewer"], r["verdict"]) for r in result["reviews"]] == [
        ("openai", "approve"),
        ("xai", "error"),
    ]
    assert [o for o in orders_for(fake_broker, "XYZ") if o.side == "buy"] == []


def test_reviewer_timeout_blocks_buys_but_not_exits(
    make_server: ServerFactory, fake_reviewer: Callable[..., Reviewer], fake_broker: FakeBroker
) -> None:
    place_filled_buy(make_server(), fake_broker, stop_price=80)  # 150 shares, room for 50 more
    server = make_server(
        [fake_reviewer("openai", TimeoutError("timed out")), fake_reviewer("xai", APPROVE)]
    )

    blocked = call_tool(server, "propose_buy", **buy_arguments())
    raised = call_tool(server, "tighten_stop", symbol="XYZ", new_stop=97)
    sold = call_tool(server, "sell", symbol="XYZ", qty=50)

    assert blocked["status"] == "blocked"
    assert blocked["reasons"][0].startswith("openai: ")
    assert (raised["status"], sold["status"]) == ("done", "done")
    assert fake_broker.get_positions()[0].qty == 100


@pytest.mark.parametrize(
    ("overrides", "changes", "qty", "stop"),
    [
        pytest.param(
            {"stop_price": 90},
            [{"stop_price": 92}, {"size_multiplier": 0.5}],
            100,
            92,
            id="raised-stop-and-half-size",
        ),
        pytest.param(
            {"limit_price": 50, "stop_price": 40, "target_price": 60},
            [{"stop_price": 45}, {}],
            300,
            45,
            id="raised-stop-never-grows-size",
        ),
        pytest.param(
            {"stop_price": 90}, [{"stop_price": 85}, {}], 200, 90, id="looser-stop-ignored"
        ),
    ],
)
@pytest.mark.parametrize("review_verdict", ["approve", "modify", "reject"])
def test_reviewer_changes_only_make_the_order_safer(
    make_server: ServerFactory,
    fake_reviewer: Callable[..., Reviewer],
    fake_broker: FakeBroker,
    review_verdict: str,
    overrides: dict[str, Any],
    changes: list[dict[str, Any]],
    qty: int,
    stop: int,
) -> None:
    reviewers = [
        fake_reviewer(name, verdict(review_verdict, **change))
        for name, change in zip(("openai", "xai"), changes, strict=True)
    ]
    server = make_server(reviewers)

    result = call_tool(server, "propose_buy", **buy_arguments(**overrides))

    assert result["status"] == "placed", result
    assert (result["order"]["qty"], dec(result["order"]["stop_price"])) == (qty, Decimal(stop))
    entry, _, stop_leg = bracket_of(fake_broker, result["order"]["order_id"])
    assert (entry.qty, stop_leg.stop_price) == (qty, Decimal(stop))


def test_packet_is_built_by_the_server_and_stored_with_both_verdicts(
    make_server: ServerFactory,
    fake_reviewer: Callable[..., Any],
    fake_broker: FakeBroker,
    state_store: StateStore,
    now: datetime,
) -> None:
    fake_broker.set_news(
        [
            NewsItem(
                id=f"n{hour}",
                headline=f"XYZ headline {hour}h ago",
                source="benzinga",
                symbols=["XYZ"],
                created_at=now - timedelta(hours=hour),
            )
            for hour in range(12)
        ]
    )
    openai_reviewer = fake_reviewer("openai", APPROVE)
    xai_reviewer = fake_reviewer("xai", verdict("modify", size_multiplier=0.5))
    server = make_server([openai_reviewer, xai_reviewer])

    result = call_tool(server, "propose_buy", **buy_arguments())

    [packet] = openai_reviewer.packets
    assert xai_reviewer.packets == [packet]
    assert (packet.proposal.symbol, packet.proposal.thesis) == (
        "XYZ",
        buy_arguments()["thesis"],
    )
    assert (packet.sized_order.qty, packet.sized_order.notional) == (200, Decimal(20_000))
    assert packet.sized_order.pct_equity == pytest.approx(0.2)
    assert packet.sized_order.risk_usd == Decimal(1000)
    assert packet.quote.ask_price == Decimal("100.02")
    assert packet.indicators.sma200 == pytest.approx(100)
    assert packet.indicators.adv20_usd == pytest.approx(100_000_000)
    assert packet.indicators.rsi14 is not None and packet.indicators.atr14 is not None
    assert len(packet.recent_closes) == 60
    assert [h.headline for h in packet.headlines] == [
        f"XYZ headline {hour}h ago" for hour in range(10)
    ]
    assert (packet.account.equity, packet.account.exposure_pct) == (Decimal(100_000), 0)
    assert packet.regime.spy_close == Decimal(100)
    assert packet.regime.spy_sma200 == pytest.approx(100)

    [review] = state_store.reviews_since(EPOCH)
    assert review.packet_hash == packet.sha256
    assert [(o.reviewer, o.verdict) for o in review.outcomes] == [
        ("openai", APPROVE),
        ("xai", verdict("modify", size_multiplier=0.5)),
    ]
    assert review.decision == "proceed"
    assert review.final_order is not None and review.final_order.qty == 100
    [entry] = call_tool(server, "get_journal")["entries"]
    assert entry["review_id"] == review.id
    assert (entry["qty"], entry["client_order_id"]) == (100, result["order"]["client_order_id"])
