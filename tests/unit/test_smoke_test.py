"""The smoke-test steps against `FakeBroker`, and the `smoke-test` subcommand's guards."""

from decimal import Decimal

import pytest

from trader_broker.brokers import BrokerError
from trader_broker.brokers.fake import FakeBroker
from trader_broker.cli import main, run_smoke_test

_STEP_NAMES = [
    "IEX quote",
    "SIP-history daily bars",
    "bracket buy accepted",
    "open orders list the legs with parent_id",
    "found by client order id",
    "cancel confirmed",
]


def test_every_step_passes_and_leaves_no_open_order(fake_broker: FakeBroker) -> None:
    checks = run_smoke_test(fake_broker, "XYZ")

    assert [check.name for check in checks] == _STEP_NAMES
    assert [check for check in checks if not check.passed] == []
    assert fake_broker.get_orders(status="open") == []


def test_test_order_is_unfillable_with_valid_stop_and_target(fake_broker: FakeBroker) -> None:
    run_smoke_test(fake_broker, "XYZ")

    entry, *legs = fake_broker.get_orders(status="all")[::-1]
    assert (entry.qty, entry.order_class, entry.limit_price) == (1, "bracket", Decimal("50.00"))
    assert {(leg.type, leg.limit_price or leg.stop_price) for leg in legs} == {
        ("limit", Decimal("60.00")),
        ("stop", Decimal("45.00")),
    }


@pytest.mark.parametrize("failing_method", ["get_orders", "get_order_by_client_id"])
def test_a_failing_step_is_reported_and_the_order_still_cancelled(
    fake_broker: FakeBroker, failing_method: str
) -> None:
    fake_broker.fail_next(failing_method, BrokerError("boom"))

    checks = run_smoke_test(fake_broker, "XYZ")

    failed = [check for check in checks if not check.passed]
    assert [check.detail for check in failed] == ["BrokerError: boom"]
    assert fake_broker.get_orders(status="open") == []


def test_a_failed_cancel_is_retried_on_the_way_out(fake_broker: FakeBroker) -> None:
    fake_broker.fail_next("cancel_order")

    checks = run_smoke_test(fake_broker, "XYZ")

    assert [check.name for check in checks if not check.passed] == ["cancel confirmed"]
    assert fake_broker.get_orders(status="open") == []


def test_an_unexpected_error_still_cancels_the_order(
    fake_broker: FakeBroker, monkeypatch: pytest.MonkeyPatch
) -> None:
    def interrupt(*args: object, **kwargs: object) -> None:
        raise KeyboardInterrupt

    monkeypatch.setattr(fake_broker, "get_orders", interrupt)

    with pytest.raises(KeyboardInterrupt):
        run_smoke_test(fake_broker, "XYZ")

    monkeypatch.undo()
    assert fake_broker.get_orders(status="all") != []
    assert fake_broker.get_orders(status="open") == []


def test_a_submit_that_times_out_after_acceptance_is_cancelled(fake_broker: FakeBroker) -> None:
    fake_broker.accept_then_time_out("submit_bracket_buy")

    checks = run_smoke_test(fake_broker, "XYZ")

    assert [check.name for check in checks if not check.passed] == ["bracket buy accepted"]
    assert fake_broker.get_orders(status="open") == []


def test_no_daily_bars_skips_the_order(fake_broker: FakeBroker) -> None:
    fake_broker.set_daily_bars("XYZ", [])

    checks = run_smoke_test(fake_broker, "XYZ")

    assert [(check.name, check.passed) for check in checks] == [
        ("IEX quote", True),
        ("SIP-history daily bars", False),
        ("bracket buy", False),
    ]
    assert fake_broker.get_orders(status="all") == []


def test_smoke_test_refuses_live_mode(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("TRADER_BROKER_MODE", "live")
    monkeypatch.setenv("TRADER_BROKER_LIVE_ACK", "I_ACCEPT_REAL_MONEY_RISK")
    monkeypatch.setenv("ALPACA_API_KEY", "key")
    monkeypatch.setenv("ALPACA_SECRET_KEY", "secret")

    assert main(["smoke-test"]) == 2
    assert "runs only in paper mode" in capsys.readouterr().err


def test_smoke_test_without_keys_exits_nonzero(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["smoke-test"]) == 2
    assert "ALPACA_API_KEY" in capsys.readouterr().err
