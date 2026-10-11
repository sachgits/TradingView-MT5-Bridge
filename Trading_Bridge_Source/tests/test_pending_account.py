"""EA v6.5 capabilities: the pending-order engine and the account snapshot.

Two additions had to land together. The pending engine means the bridge can now
ask the EA to place, cancel and modify resting orders - real orders that sit on
the book until triggered - so their parameters are validated as strictly as a
market entry's. The account action is the opposite: a read-only state query that
must never be mistaken for a trade, never create a ledger row, and never delay
one.
"""
import json

import pytest

from bridge import contract, events, schema, validator
from bridge.web import create_app

from conftest import BUY


def normalize(payload):
    return schema.normalize_command(payload)


def check(payload, mode=validator.MODE_STRICT):
    command = normalize(payload)
    assert "error" not in command, command
    return command, validator.validate_command(command, mode)


def pending(action="buy_stop", **extra):
    base = {"action": action, "symbol": "EURUSD", "size": {"value": 0.01, "unit": "lots"}, "price": 1.0950}
    base.update(extra)
    return base


# ------------------------------------------------------------------ contract

def test_the_pending_engine_is_a_real_action_not_a_skipped_one():
    for action in ("buy_limit", "sell_limit", "buy_stop", "sell_stop",
                   "buy_stop_limit", "sell_stop_limit", "cancel_pending", "modify_pending"):
        assert action in contract.EXECUTED_ACTIONS
        assert action not in contract.SKIPPED_ACTIONS


def test_account_is_executed_and_needs_no_symbol():
    assert "account" in contract.EXECUTED_ACTIONS
    _, report = check({"action": "account"})
    assert report["valid"], report["errors"]


def test_the_v65_pending_guard_codes_are_declared():
    for code in (-40, -41, -42, -43):
        assert code in contract.LOCAL_RETCODES
    assert contract.describe_retcode(-41).startswith("no matching pending")


def test_public_contract_exposes_the_pending_vocabulary():
    public = contract.public_contract()
    assert set(public["pending_actions"]) >= {"buy_stop", "sell_limit", "pending"}
    assert public["stop_limit_actions"] == ["buy_stop_limit", "sell_stop_limit"]
    assert "order_ticket" in public["pending_fields"]
    assert public["price_units"] == ["distance", "points", "price"]


# ------------------------------------------------------------------ schema

def test_pending_field_aliases_fold_onto_the_canonical_key():
    command, report = check({"action": "buy_stop", "symbol": "EURUSD",
                             "size": {"value": 0.01, "unit": "lots"},
                             "entry": 1.09, "stop_limit": 1.08, "ticket": 42, "expiry": "2026-12-31T23:59:00"})
    assert report["valid"], report["errors"]
    assert command["price"] == 1.09
    assert command["stoplimit"] == 1.08
    assert command["order_ticket"] == 42
    assert command["expiration"] == "2026-12-31T23:59:00"
    for alias in ("entry", "stop_limit", "ticket", "expiry"):
        assert alias not in command


def test_order_type_is_canonicalized_like_an_action():
    command, _ = check({"action": "pending", "order_type": "BuyStop", "symbol": "EURUSD",
                        "size": {"value": 0.01, "unit": "lots"}, "price": 1.1})
    assert command["order_type"] == "buy_stop"


def test_price_unit_is_lowercased():
    command, _ = check(pending(price_unit="POINTS"))
    assert command["price_unit"] == "points"


# ---------------------------------------------------------------- validator

def test_a_pending_order_requires_a_price():
    _, report = check({"action": "buy_stop", "symbol": "EURUSD", "size": {"value": 0.01, "unit": "lots"}})
    assert not report["valid"]
    assert any("price" in e for e in report["errors"])


def test_a_signed_distance_price_is_accepted_for_pending():
    _, report = check(pending(price=-120, price_unit="points"))
    assert report["valid"], report["errors"]


def test_an_absolute_pending_price_must_be_positive():
    _, report = check(pending(price=-1.0))
    assert not report["valid"]
    assert any("greater than zero" in e for e in report["errors"])


def test_a_zero_distance_price_is_refused():
    # 0 points would rest exactly at the market, which is a market order the
    # sender did not ask for.
    _, report = check(pending(price=0, price_unit="points"))
    assert not report["valid"]


def test_stop_limit_types_require_the_inner_limit_price():
    _, report = check(pending(action="buy_stop_limit"))
    assert not report["valid"]
    assert any("stoplimit" in e for e in report["errors"])

    _, ok = check(pending(action="buy_stop_limit", stoplimit=1.0940))
    assert ok["valid"], ok["errors"]


def test_action_pending_requires_a_pending_order_type():
    _, report = check({"action": "pending", "symbol": "EURUSD",
                       "size": {"value": 0.01, "unit": "lots"}, "price": 1.1})
    assert not report["valid"]
    assert any("order_type" in e for e in report["errors"])


def test_an_unknown_price_unit_is_refused():
    _, report = check(pending(price_unit="percent"))
    assert not report["valid"]
    assert any("price_unit" in e for e in report["errors"])


def test_expiration_must_parse_the_way_the_ea_parses_it():
    _, iso = check(pending(expiration="2026-12-31T23:59:00"))
    assert iso["valid"], iso["errors"]
    _, mt5 = check(pending(expiration="2026.12.31 23:59"))
    assert mt5["valid"], mt5["errors"]
    _, bad = check(pending(expiration="next friday"))
    assert not bad["valid"]
    assert any("expiration" in e for e in bad["errors"])


def test_pending_sltp_are_flagged_as_measured_from_the_order_price():
    _, report = check(pending(stop_loss={"unit": "points", "value": 200}))
    assert report["valid"], report["errors"]
    assert any("order price" in w for w in report["warnings"])


def test_a_market_entry_price_is_a_warning_but_a_pending_price_is_not():
    _, market = check({**BUY, "price": 1.09})
    assert any("market" in w for w in market["warnings"])
    _, pend = check(pending())
    assert not any("ignored for a market entry" in w for w in pend["warnings"])


def test_cancel_pending_needs_a_ticket_or_a_symbol():
    _, report = check({"action": "cancel_pending"})
    assert not report["valid"]
    _, by_ticket = check({"action": "cancel_pending", "order_ticket": 7})
    assert by_ticket["valid"], by_ticket["errors"]
    _, by_symbol = check({"action": "cancel_pending", "symbol": "EURUSD"})
    assert by_symbol["valid"], by_symbol["errors"]


def test_modify_pending_needs_something_to_change():
    _, report = check({"action": "modify_pending", "order_ticket": 7})
    assert not report["valid"]
    assert any("at least one" in e for e in report["errors"])
    _, ok = check({"action": "modify_pending", "order_ticket": 7, "price": 1.0975})
    assert ok["valid"], ok["errors"]


# ------------------------------------------------- account snapshot (service)

def enable_account_polling(service, seconds=30):
    service.settings.account_poll_seconds = seconds


def test_an_account_request_is_injected_only_when_the_queue_is_empty(service):
    enable_account_polling(service)
    envelope = service.ea_poll()
    assert envelope["status"] == contract.ENVELOPE_NEW
    assert envelope["normalized"] == {"action": "account"}

    # A second poll must not ask again while the first is outstanding.
    assert service.ea_poll()["status"] == contract.ENVELOPE_IDLE


def test_account_polling_can_be_disabled(service):
    enable_account_polling(service, 0)
    assert service.ea_poll()["status"] == contract.ENVELOPE_IDLE


def test_a_queued_trade_takes_the_poll_over_an_account_request(service):
    enable_account_polling(service)
    service.submit(dict(BUY))
    envelope = service.ea_poll()
    assert envelope["normalized"]["action"] == "buy"


def test_the_account_envelope_is_unique_so_the_ea_does_not_dedup_it(service):
    enable_account_polling(service)
    first = service.ea_poll()
    service.ea_ack({"request_id": first["request_id"], "status": "SUCCESS", "retcode": 0,
                    "action": "account", "snapshot": {"account": 1}})
    # The snapshot is now fresh, so force the next ask rather than waiting for
    # the age gate; what matters here is that two asks never look alike.
    service.request_account_refresh()
    second = service.ea_poll()
    assert second["normalized"] == {"action": "account"}
    assert second["request_id"] != first["request_id"]
    # The EA dedups on the whole body; a fresh id and timestamp keep it unique.
    assert json.dumps(first, sort_keys=True) != json.dumps(second, sort_keys=True)


def test_an_account_ack_stores_the_snapshot_and_creates_no_ledger_row(service, store):
    enable_account_polling(service)
    request_id = service.ea_poll()["request_id"]
    snapshot = {"account": 5123, "balance": 100.0, "positions_count": 0, "positions": [],
                "pending_orders_count": 0, "pending_orders": []}
    result = service.ea_ack({"request_id": request_id, "status": "SUCCESS", "retcode": 0,
                             "action": "account", "snapshot": snapshot})
    assert result["account"] is True and result["stored"] is True
    assert store.get_signal(request_id) is None
    stored, at = store.load_account_snapshot()
    assert stored == snapshot and at


def test_an_account_ack_does_not_disturb_a_real_signal(service, store):
    service.submit(dict(BUY))
    trade_id = service.ea_poll()["request_id"]
    service.ea_ack({"request_id": "acct-other", "status": "SUCCESS", "retcode": 0,
                    "action": "account", "snapshot": {"account": 9}})
    # The trade is still in flight; the unrelated account ack changed nothing.
    assert store.get_signal(trade_id)["status"] == contract.STATUS_DISPATCHED


def test_account_state_reports_age_and_in_flight(service):
    enable_account_polling(service)
    assert service.account_state()["snapshot"] is None
    request_id = service.ea_poll()["request_id"]
    assert service.account_state()["in_flight"] is True
    service.ea_ack({"request_id": request_id, "status": "SUCCESS", "retcode": 0,
                    "action": "account", "snapshot": {"account": 1}})
    state = service.account_state()
    assert state["in_flight"] is False
    assert state["age_seconds"] is not None and state["age_seconds"] >= 0


def test_a_forced_refresh_asks_even_when_the_snapshot_is_fresh(service):
    enable_account_polling(service, 3600)
    request_id = service.ea_poll()["request_id"]
    service.ea_ack({"request_id": request_id, "status": "SUCCESS", "retcode": 0,
                    "action": "account", "snapshot": {"account": 1}})
    assert service.ea_poll()["status"] == contract.ENVELOPE_IDLE  # fresh, not due
    assert service.request_account_refresh()["ok"] is True
    assert service.ea_poll()["normalized"] == {"action": "account"}


def test_pause_suppresses_account_requests(service):
    enable_account_polling(service)
    service.update_settings({"paused": True})
    envelope = service.ea_poll()
    assert envelope["status"] == contract.ENVELOPE_IDLE
    assert envelope.get("reason") == "paused"


# ------------------------------------------------------- account over HTTP

@pytest.fixture()
def client(service):
    app = create_app(service)
    app.config["TESTING"] = True
    return app.test_client()


def seed_snapshot(service):
    enable_account_polling(service)
    request_id = service.ea_poll()["request_id"]
    service.ea_ack({"request_id": request_id, "status": "SUCCESS", "retcode": 0,
                    "action": "account",
                    "snapshot": {"account": 5123, "currency": "USD", "balance": 250.0,
                                 "positions_count": 1,
                                 "positions": [{"ticket": 1, "symbol": "EURUSD", "type": "buy",
                                                "volume": 0.1, "profit": 2.0}],
                                 "pending_orders_count": 0, "pending_orders": []}})


def test_the_account_endpoint_returns_the_stored_snapshot(client, service):
    seed_snapshot(service)
    body = client.get("/api/v1/account").get_json()
    assert body["snapshot"]["account"] == 5123
    assert body["poll_seconds"] == service.settings.account_poll_seconds


def test_the_positions_endpoint_lifts_rows_out_of_the_snapshot(client, service):
    seed_snapshot(service)
    body = client.get("/api/v1/positions").get_json()
    assert body["positions_count"] == 1
    assert body["positions"][0]["symbol"] == "EURUSD"
    assert body["balance"] == 250.0
    assert body["pending_orders"] == []


def test_positions_without_a_snapshot_is_empty_not_an_error(client, service):
    body = client.get("/api/v1/positions").get_json()
    assert body["positions"] == []
    assert body["pending_orders"] == []
    assert body["positions_count"] == 0
    assert body["balance"] is None


def test_the_state_snapshot_carries_the_account_for_the_board(client, service):
    seed_snapshot(service)
    body = client.get("/api/v1/state").get_json()
    assert body["account"]["snapshot"]["account"] == 5123


def test_refresh_is_guarded_like_intake(client, service):
    response = client.post("/api/v1/account/refresh",
                           headers={"Origin": "https://evil.example"})
    assert response.status_code == 403
    assert client.post("/api/v1/account/refresh").get_json()["ok"] is True
