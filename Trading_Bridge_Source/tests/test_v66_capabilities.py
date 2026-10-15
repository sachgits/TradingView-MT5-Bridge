"""Tests for EA v6.6 capabilities."""
import pytest
from bridge import contract, schema, validator
from conftest import BUY


def normalize(payload):
    return schema.normalize_command(payload)


def check(payload, mode=validator.MODE_STRICT):
    command = normalize(payload)
    assert "error" not in command, command
    return command, validator.validate_command(command, mode)


def test_ea_version_is_6_6():
    assert contract.EA_VERSION == "6.6"


def test_trailing_is_executed_action():
    assert "trailing" in contract.EXECUTED_ACTIONS
    assert "trailing" in contract.MANAGEMENT_ACTIONS
    assert contract.normalize_action("trail") == "trailing"
    assert contract.normalize_action("trailing_stop") == "trailing"


def test_trailing_validation():
    # Valid with symbol and trailing distance
    cmd, report = check({
        "action": "trailing",
        "symbol": "EURUSD",
        "trailing": {"distance": 50, "activation": 20},
    })
    assert report["valid"], report["errors"]
    assert cmd["trailing"]["distance"]["unit"] == "points"
    assert cmd["trailing"]["distance"]["value"] == 50
    assert cmd["trailing"]["activation"]["value"] == 20

    # Valid with position_ticket instead of symbol
    cmd, report = check({
        "action": "trailing",
        "position_ticket": 123456,
        "trailing": {"distance": 40},
    })
    assert report["valid"], report["errors"]


def test_risk_money_sizing():
    # Valid in strict mode when stop_loss is provided
    cmd, report = check({
        "action": "buy",
        "symbol": "EURUSD",
        "size": {"unit": "risk_money", "value": 150.0},
        "stop_loss": 50,
    })
    assert report["valid"], report["errors"]
    assert cmd["size"]["unit"] == "risk_money"
    assert cmd["size"]["value"] == 150.0

    # Aliases fold to risk_money
    cmd2 = normalize({
        "action": "buy",
        "symbol": "EURUSD",
        "size": {"unit": "risk_usd", "value": 100},
        "stop_loss": 50,
    })
    assert cmd2["size"]["unit"] == "risk_money"

    # Missing stop_loss in strict mode is rejected
    _, strict_err = check({
        "action": "buy",
        "symbol": "EURUSD",
        "size": {"unit": "risk_money", "value": 150.0},
    })
    assert not strict_err["valid"]
    assert any("stop_loss is required" in e for e in strict_err["errors"])

    # Permissive mode warns instead of rejecting
    _, perm = check({
        "action": "buy",
        "symbol": "EURUSD",
        "size": {"unit": "risk_money", "value": 150.0},
    }, validator.MODE_PERMISSIVE)
    assert perm["valid"]
    assert any("stop_loss omitted" in w for w in perm["warnings"])


def test_position_ticket_targeting():
    # close_position with ticket
    cmd, report = check({
        "action": "close_position",
        "position_ticket": 78910,
    })
    assert report["valid"], report["errors"]
    assert cmd["position_ticket"] == 78910

    # 'ticket' alias folds to position_ticket for position management
    cmd_alias = normalize({
        "action": "close_position",
        "ticket": 78910,
    })
    assert cmd_alias["position_ticket"] == 78910
    assert "ticket" not in cmd_alias

    # modify with position_ticket
    cmd_mod, rep_mod = check({
        "action": "modify",
        "position_ticket": 78910,
        "stop_loss": 100,
    })
    assert rep_mod["valid"], rep_mod["errors"]

    # breakeven with position_ticket
    cmd_be, rep_be = check({
        "action": "breakeven",
        "position_ticket": 78910,
        "breakeven": {"lock_in": 15, "trigger": 30},
    })
    assert rep_be["valid"], rep_be["errors"]


def test_partial_close_close_volume():
    # Exact lots volume for partial close
    cmd, report = check({
        "action": "partial_close",
        "symbol": "EURUSD",
        "close_volume": 0.05,
    })
    assert report["valid"], report["errors"]
    assert cmd["close_volume"] == 0.05

    # Invalid close_volume <= 0
    _, err = check({
        "action": "partial_close",
        "symbol": "EURUSD",
        "close_volume": -0.1,
    })
    assert not err["valid"]
    assert any("close_volume must be greater than 0" in e for e in err["errors"])


def test_breakeven_trigger_enforcement():
    cmd, report = check({
        "action": "breakeven",
        "symbol": "EURUSD",
        "breakeven": {"lock_in": 10, "trigger": {"unit": "points", "value": 40}},
    })
    assert report["valid"], report["errors"]
    assert cmd["breakeven"]["trigger"]["value"] == 40


def test_deviation_and_comment_known():
    cmd = normalize({
        **BUY,
        "deviation": 20,
        "comment": "Algo Trade 1",
    })
    assert cmd["deviation"] == 20
    assert cmd["comment"] == "Algo Trade 1"
    assert "deviation" not in cmd["_meta"]["unknown_fields"]
    assert "comment" not in cmd["_meta"]["unknown_fields"]


def test_result_extra_fields():
    assert "position_ticket" in contract.RESULT_EXTRA_FIELDS
    assert "deal_ticket" in contract.RESULT_EXTRA_FIELDS
    assert "filled_price" in contract.RESULT_EXTRA_FIELDS
    assert "filled_volume" in contract.RESULT_EXTRA_FIELDS


def test_result_extra_round_trips_through_an_ack(service, store):
    """The broker objects the EA reports must land in the ledger, not be dropped.

    Membership in RESULT_EXTRA_FIELDS is not enough on its own - the field has
    to survive ea_ack -> record_result -> the row the board reads back.
    """
    rid = service.submit(dict(BUY))["request_id"]
    service.ea_ack({
        "request_id": rid,
        "status": "SUCCESS",
        "retcode": 0,
        "result": "BUY OPENED",
        "order_ticket": 555,
        "position_ticket": 777,
        "deal_ticket": 888,
        "filled_price": 1.08421,
        "filled_volume": 0.01,
    })
    row = store.get_signal(rid)
    assert row["status"] == contract.STATUS_EXECUTED
    # Parsed back from JSON by the store, so it is a dict rather than a string.
    assert row["result_extra"] == {
        "order_ticket": 555,
        "position_ticket": 777,
        "deal_ticket": 888,
        "filled_price": 1.08421,
        "filled_volume": 0.01,
    }


def test_an_ack_without_extras_stores_no_result_extra(service, store):
    rid = service.submit(dict(BUY))["request_id"]
    service.ea_ack({"request_id": rid, "status": "SUCCESS", "retcode": 0, "result": "OK"})
    assert store.get_signal(rid)["result_extra"] is None


def test_a_snapshot_ack_never_writes_a_result_extra_row(service, store):
    # An account snapshot is transient telemetry: it is intercepted before the
    # trade path and must not create a ledger row carrying its payload.
    service.ea_ack({"request_id": "acct-1", "action": "account", "status": "SUCCESS",
                    "retcode": 0, "snapshot": {"balance": 1000.0}})
    assert store.get_signal("acct-1") is None


def test_retcode_minus_50():
    assert contract.describe_retcode(-50) == "unparseable command or invalid symbol rejected locally"
