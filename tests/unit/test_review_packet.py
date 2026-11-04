"""The review packet, and OpenAICompatReviewer against a stubbed openai client."""

import json
from decimal import Decimal
from typing import Any
from unittest.mock import MagicMock

import httpx
import pytest
from openai import APITimeoutError, OpenAI
from openai.types.chat import ChatCompletion

from trader_broker.config import ConfigError, Settings
from trader_broker.review import SYSTEM_PROMPT, OpenAICompatReviewer, ReviewPacket

_VALID_REPLY = {
    "verdict": "modify",
    "confidence": "medium",
    "reasons": ["Extended above the 20-day average."],
    "modifications": {"stop_price": 96, "size_multiplier": 0.5, "target_price": 110},
}


def test_packet_has_every_field(review_packet: ReviewPacket) -> None:
    packet = json.loads(review_packet.canonical_json())

    assert packet["proposal"] == {
        "symbol": "XYZ",
        "stop_price": "95",
        "target_price": "120",
        "thesis": "Breakout above a six-month base on rising volume.",
        "invalidation": "A close back inside the base.",
        "horizon_days": 20,
        "limit_price": "100",
    }
    for name in ("qty", "notional", "pct_equity", "risk_usd", "pct_equity_risk"):
        assert name in packet["sized_order"]
    assert packet["quote"]["bid_price"] == "99.98"
    assert packet["quote"]["ask_price"] == "100.02"
    assert packet["indicators"] == {
        "close": "100",
        "sma20": 100.0,
        "sma50": 100.0,
        "sma200": 100.0,
        "rsi14": 50.0,
        "atr14": 2.0,
        "pct_from_52w_high": pytest.approx(100 / 101 - 1),
        "adv20_usd": 100_000_000.0,
    }
    assert packet["account"] == {
        "equity": "100000",
        "cash": "80000",
        "exposure_pct": 0.2,
        "positions": [
            {"symbol": "ABC", "qty": 200, "market_value": "20000", "pct_equity": 0.2},
        ],
    }
    assert packet["regime"] == {
        "spy_close": "100",
        "spy_sma50": 100.0,
        "spy_sma200": 100.0,
        "above_sma50": False,
        "above_sma200": False,
    }


def test_packet_lists_the_last_60_closes(review_packet: ReviewPacket) -> None:
    assert len(review_packet.recent_closes) == 60


def test_packet_keeps_the_10_latest_headlines_with_timestamps(
    review_packet: ReviewPacket,
) -> None:
    headlines = review_packet.headlines

    assert [h.headline for h in headlines] == [f"XYZ headline {hour}" for hour in range(10)]
    assert all(h.created_at.tzinfo is not None for h in headlines)


def test_same_input_gives_the_same_hash(review_packet: ReviewPacket) -> None:
    rebuilt = ReviewPacket.model_validate_json(review_packet.model_dump_json())

    assert rebuilt.sha256 == review_packet.sha256
    assert len(review_packet.sha256) == 64


def test_changed_input_changes_the_hash(review_packet: ReviewPacket) -> None:
    changed = review_packet.model_copy(update={"recent_closes": review_packet.recent_closes[1:]})

    assert changed.sha256 != review_packet.sha256


def _completion(content: str) -> ChatCompletion:
    return ChatCompletion.model_validate(
        {
            "id": "chatcmpl-1",
            "object": "chat.completion",
            "created": 1_793_721_600,
            "model": "gpt-test",
            "choices": [
                {
                    "index": 0,
                    "finish_reason": "stop",
                    "message": {"role": "assistant", "content": content},
                }
            ],
        }
    )


def _reviewer(*replies: str | Exception) -> tuple[OpenAICompatReviewer, MagicMock]:
    client = MagicMock(spec=OpenAI)
    create = client.chat.completions.create
    create.side_effect = [
        reply if isinstance(reply, Exception) else _completion(reply) for reply in replies
    ]
    return OpenAICompatReviewer("openai", client, "gpt-test"), create


def _timeout() -> APITimeoutError:
    return APITimeoutError(httpx.Request("POST", "https://api.openai.com/v1/chat/completions"))


def test_valid_reply_is_parsed(review_packet: ReviewPacket) -> None:
    reviewer, create = _reviewer(json.dumps(_VALID_REPLY))

    outcome = reviewer.review(review_packet)

    assert outcome.reviewer == "openai"
    assert outcome.verdict is not None
    assert outcome.verdict.verdict == "modify"
    assert outcome.verdict.modifications is not None
    assert outcome.verdict.modifications.size_multiplier == 0.5
    assert outcome.verdict.modifications.target_price == Decimal(110)
    request: dict[str, Any] = create.call_args.kwargs
    assert request["model"] == "gpt-test"
    assert request["response_format"] == {"type": "json_object"}
    assert request["messages"] == [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": review_packet.canonical_json()},
    ]


def test_invalid_json_then_valid_succeeds_after_one_retry(review_packet: ReviewPacket) -> None:
    reviewer, create = _reviewer("not json", json.dumps(_VALID_REPLY))

    outcome = reviewer.review(review_packet)

    assert outcome.verdict is not None
    assert create.call_count == 2


def test_api_error_then_valid_succeeds_after_one_retry(review_packet: ReviewPacket) -> None:
    reviewer, create = _reviewer(_timeout(), json.dumps(_VALID_REPLY))

    assert reviewer.review(review_packet).verdict is not None
    assert create.call_count == 2


def test_invalid_twice_is_an_error(review_packet: ReviewPacket) -> None:
    reviewer, create = _reviewer("not json", "{}")

    outcome = reviewer.review(review_packet)

    assert outcome.verdict is None
    assert outcome.error is not None
    assert outcome.error.startswith("invalid verdict")
    assert outcome.error.endswith("(after one retry)")
    assert create.call_count == 2


def test_api_error_twice_is_an_error(review_packet: ReviewPacket) -> None:
    reviewer, _ = _reviewer(_timeout(), _timeout())

    outcome = reviewer.review(review_packet)

    assert outcome.error == "API error: APITimeoutError: Request timed out. (after one retry)"


@pytest.mark.parametrize("multiplier", [0, 1.5, -0.5])
def test_out_of_range_size_multiplier_is_invalid(
    review_packet: ReviewPacket, multiplier: float
) -> None:
    reply = json.dumps({**_VALID_REPLY, "modifications": {"size_multiplier": multiplier}})
    reviewer, create = _reviewer(reply, reply)

    outcome = reviewer.review(review_packet)

    assert outcome.error is not None
    assert create.call_count == 2


def test_from_settings_configures_the_client() -> None:
    settings = Settings.from_env(
        {"REVIEWER_2_MODEL": "grok-test", "XAI_API_KEY": "xai-secret"}
    ).reviewer_2

    reviewer = OpenAICompatReviewer.from_settings(settings, timeout_s=45)

    client = reviewer._client
    assert reviewer.name == "xai"
    assert str(client.base_url).rstrip("/") == "https://api.x.ai/v1"
    assert client.api_key == "xai-secret"
    assert client.timeout == 45
    assert client.max_retries == 0


def test_from_settings_needs_a_model_and_key() -> None:
    with pytest.raises(ConfigError, match="XAI_API_KEY"):
        OpenAICompatReviewer.from_settings(Settings().reviewer_2, timeout_s=60)
