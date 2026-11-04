from datetime import datetime, timedelta
from decimal import Decimal

import pytest

from trader_broker.brokers import BrokerError, RateLimitError, UnknownResultError
from trader_broker.brokers.fake import FakeBroker
from trader_broker.models import BracketOrderRequest, Order


def _bracket(client_order_id: str = "tb-1", qty: int = 10) -> BracketOrderRequest:
    return BracketOrderRequest(
        symbol="XYZ",
        qty=qty,
        limit_price=Decimal("100.50"),
        stop_price=Decimal(90),
        take_profit_price=Decimal(120),
        client_order_id=client_order_id,
    )


def _legs(broker: FakeBroker, entry: Order) -> dict[str, Order]:
    """The entry's legs keyed by order type: `limit` (take-profit) and `stop`."""
    return {o.type: o for o in broker.get_orders("all") if o.parent_id == entry.id}


def _by_client_id(broker: FakeBroker, client_order_id: str) -> Order:
    order = broker.get_order_by_client_id(client_order_id)
    assert order is not None
    return order


def test_bracket_buy_creates_entry_and_held_legs(fake_broker: FakeBroker) -> None:
    entry = fake_broker.submit_bracket_buy(_bracket())

    legs = _legs(fake_broker, entry)
    assert (entry.side, entry.type, entry.order_class, entry.status) == (
        "buy",
        "limit",
        "bracket",
        "new",
    )
    assert entry.time_in_force == "gtc"
    assert legs["limit"].limit_price == Decimal(120)
    assert legs["stop"].stop_price == Decimal(90)
    assert {leg.status for leg in legs.values()} == {"held"}
    assert fake_broker.get_order_by_client_id("tb-1") == entry


def test_bracket_buy_rejects_duplicate_client_id_and_bad_prices(
    fake_broker: FakeBroker,
) -> None:
    fake_broker.submit_bracket_buy(_bracket())

    with pytest.raises(BrokerError, match="unique"):
        fake_broker.submit_bracket_buy(_bracket())
    with pytest.raises(BrokerError, match="stop_price"):
        fake_broker.submit_bracket_buy(
            BracketOrderRequest("XYZ", 1, Decimal(100), Decimal(100), Decimal(110), "tb-2")
        )


def test_partial_fill_buys_shares_but_leaves_the_legs_inactive(
    fake_broker: FakeBroker,
) -> None:
    entry = fake_broker.submit_bracket_buy(_bracket())

    fill = fake_broker.fill_order(entry.id, qty=4)

    assert fill.qty == 4 and fill.price == Decimal("100.50")
    assert _by_client_id(fake_broker, "tb-1").status == "partially_filled"
    [position] = fake_broker.get_positions()
    assert (position.symbol, position.qty) == ("XYZ", 4)
    assert fake_broker.get_account().cash == Decimal(100_000) - 4 * Decimal("100.50")
    assert _legs(fake_broker, entry)["limit"].status == "held"
    # Inactive legs reserve nothing, so the filled shares can get their own stop.
    fake_broker.submit_stop("XYZ", 4, Decimal(90), "tb-repair")
    with pytest.raises(ValueError, match="can fill"):
        fake_broker.fill_order(_legs(fake_broker, entry)["stop"].id)


def test_complete_fill_activates_the_take_profit(fake_broker: FakeBroker) -> None:
    entry = fake_broker.submit_bracket_buy(_bracket())

    fake_broker.fill_order(entry.id, qty=4)
    fake_broker.fill_order(entry.id)

    assert _legs(fake_broker, entry)["limit"].status == "new"


def test_trigger_stop_sells_filled_shares_at_gap_price_and_cancels_take_profit(
    fake_broker: FakeBroker,
) -> None:
    entry = fake_broker.submit_bracket_buy(_bracket())
    fake_broker.fill_order(entry.id)

    [fill] = fake_broker.trigger_stop("XYZ", price=Decimal(85))

    assert (fill.side, fill.qty, fill.price) == ("sell", 10, Decimal(85))
    assert fake_broker.get_positions() == []
    legs = _legs(fake_broker, entry)
    assert (legs["stop"].status, legs["limit"].status) == ("filled", "canceled")
    assert fake_broker.get_orders("open") == []


def test_sell_beyond_qty_reserved_by_bracket_legs_is_rejected(
    fake_broker: FakeBroker,
) -> None:
    entry = fake_broker.submit_bracket_buy(_bracket())
    fake_broker.fill_order(entry.id)

    with pytest.raises(BrokerError, match=r"requested: 1, available: 0"):
        fake_broker.submit_sell("XYZ", 1, "tb-sell")

    for leg in _legs(fake_broker, entry).values():
        fake_broker.cancel_order(leg.id)
    sell = fake_broker.submit_sell("XYZ", 10, "tb-sell")
    assert (sell.status, sell.filled_qty, sell.filled_avg_price) == ("filled", 10, Decimal("99.98"))


def test_open_stop_reserves_its_qty(fake_broker: FakeBroker) -> None:
    fake_broker.set_position("XYZ", 10, Decimal(100))
    fake_broker.submit_stop("XYZ", 6, Decimal(90), "tb-stop")

    with pytest.raises(BrokerError, match=r"requested: 5, available: 4"):
        fake_broker.submit_sell("XYZ", 5, "tb-sell")
    fake_broker.submit_sell("XYZ", 4, "tb-sell")
    assert fake_broker.get_positions()[0].qty == 6


def test_cancelling_unfilled_entry_cancels_legs_but_partial_fill_activates_them(
    fake_broker: FakeBroker,
) -> None:
    unfilled = fake_broker.submit_bracket_buy(_bracket("tb-1"))
    partly = fake_broker.submit_bracket_buy(_bracket("tb-2"))
    fake_broker.fill_order(partly.id, qty=3)

    fake_broker.cancel_order(unfilled.id)
    fake_broker.cancel_order(partly.id)

    assert {leg.status for leg in _legs(fake_broker, unfilled).values()} == {"canceled"}
    legs = _legs(fake_broker, partly)
    assert legs["stop"].is_open and legs["limit"].status == "new"
    assert fake_broker.fill_order(legs["stop"].id).qty == 3
    with pytest.raises(BrokerError, match="not cancelable"):
        fake_broker.cancel_order(unfilled.id)


def test_replace_stop_creates_new_order(fake_broker: FakeBroker) -> None:
    fake_broker.set_position("XYZ", 10, Decimal(100))
    old = fake_broker.submit_stop("XYZ", 10, Decimal(90), "tb-stop")

    new = fake_broker.replace_stop(old.id, Decimal(95), "tb-raised")

    assert new.id != old.id and new.stop_price == Decimal(95) and new.is_open
    assert new.client_order_id == "tb-raised"
    assert _by_client_id(fake_broker, "tb-stop").status == "replaced"


def test_delayed_cancel_reads_pending_cancel_then_canceled(fake_broker: FakeBroker) -> None:
    fake_broker.set_position("XYZ", 10, Decimal(100))
    stop = fake_broker.submit_stop("XYZ", 10, Decimal(90), "tb-stop")
    fake_broker.delay_cancels(reads=2)

    fake_broker.cancel_order(stop.id)

    statuses = [fake_broker.get_orders("all")[0].status for _ in range(3)]
    assert statuses == ["pending_cancel", "pending_cancel", "canceled"]


def test_account_equity_follows_prices(fake_broker: FakeBroker) -> None:
    fake_broker.set_position("XYZ", 100, Decimal(100), current_price=Decimal(110))

    account = fake_broker.get_account()

    assert account.equity == Decimal(100_000 + 11_000)
    assert account.last_equity == Decimal(100_000)


def test_fail_next_raises_once(fake_broker: FakeBroker) -> None:
    fake_broker.fail_next("get_quote", RateLimitError("429"))
    fake_broker.fail_next("get_account")

    with pytest.raises(RateLimitError):
        fake_broker.get_quote("XYZ")
    with pytest.raises(BrokerError):
        fake_broker.get_account()
    assert fake_broker.get_quote("XYZ").ask_price == Decimal("100.02")


def test_failed_submit_stores_nothing(fake_broker: FakeBroker) -> None:
    fake_broker.fail_next("submit_bracket_buy", UnknownResultError("timeout"))

    with pytest.raises(UnknownResultError):
        fake_broker.submit_bracket_buy(_bracket())
    assert fake_broker.get_order_by_client_id("tb-1") is None


def test_accept_then_time_out_stores_the_order(fake_broker: FakeBroker) -> None:
    fake_broker.accept_then_time_out("submit_bracket_buy")

    with pytest.raises(UnknownResultError):
        fake_broker.submit_bracket_buy(_bracket())
    assert fake_broker.get_order_by_client_id("tb-1") is not None


def test_scripting_rejects_unknown_methods(fake_broker: FakeBroker) -> None:
    with pytest.raises(ValueError):
        fake_broker.fail_next("get_acount")
    with pytest.raises(ValueError):
        fake_broker.accept_then_time_out("get_account")


def test_orders_and_fills_carry_the_injected_time(fake_broker: FakeBroker, now: datetime) -> None:
    later = now + timedelta(minutes=31)
    fake_broker.set_now(later)
    entry = fake_broker.submit_bracket_buy(_bracket())
    fake_broker.fill_order(entry.id)

    assert entry.created_at == later
    assert [fill.filled_at for fill in fake_broker.get_fills(since=now)] == [later]
    assert fake_broker.get_fills(since=later + timedelta(seconds=1)) == []
