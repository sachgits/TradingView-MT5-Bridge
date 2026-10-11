"""Trade-journal JSON is extracted into a lean EA command; the inbox is untouched."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone

from bridge import schema, validator
from bridge.service import public_command

from conftest import BUY


def fresh(record, seconds_ago=0):
    """A journal record is a *now* signal, so fixtures need a live trigger stamp.

    The bridge refuses a trigger older than journal_max_age_seconds: history left
    in a shared inbox must not become a burst of market orders.
    """
    out = deepcopy(record)
    stamp = (datetime.now(timezone.utc).replace(microsecond=0)
             - timedelta(seconds=seconds_ago)).isoformat()
    out["lifecycle"]["generated_at"] = stamp
    out["lifecycle"]["triggered_at"] = stamp
    return out

FLO = {
    "id": "20260922T195825Z_FLO_GBPCHF_long",
    "book": "FLO",
    "playbook": "Flossbach Liquidation Sweep",
    "tag": "FLO",
    "pair": "GBPCHF",
    "direction": "long",
    "tier": "solid",
    "status": "triggered_active",
    "confluence": ["raw_status=triggered_active"],
    "setup": {"entry": 1.0955599999999999, "stop": 1.09335, "target": 1.0999799999999995},
    "sizing": {
        "lots": 0.02,
        "pip_distance": 22.1,
        "risk_usd_target": 5,
        "risk_usd_actual": 4.99,
        "floored_to_min_lot": False,
        "graduation_stage_usd": 5,
        "account_balance_at_entry_usd": 50.0,
    },
    "management": {
        "atr_context": None,
        "breakeven": {"applicable": True, "trigger_r": 1.0, "triggered": False, "triggered_at": None},
    },
    "comments": [],
    "adjustments": [],
    "lifecycle": {
        "generated_at": "2026-09-22T19:58:25.901698+00:00",
        "triggered_at": "2026-09-22T19:58:25.901698+00:00",
        "closed_at": None,
        "duration_seconds": None,
        "duration_human": None,
    },
    "resolution": {"outcome": None, "exit_price": None, "realized_pnl_usd": None, "realized_r": None},
}

G8_SHORT = {
    "id": "20260923T075114Z_G8_GBPUSD_short",
    "book": "G8",
    "playbook": "G8 Inside-Day Breakout",
    "tag": "G8",
    "pair": "GBPUSD",
    "direction": "short",
    "tier": "strong",
    "status": "triggered_active",
    "confluence": ["raw_status=triggered_short"],
    "setup": {"entry": 1.33216, "stop": 1.33873, "target": None},
    "sizing": {
        "lots": 0.01,
        "pip_distance": 65.7,
        "risk_usd_target": 5,
        "risk_usd_actual": 6.57,
        "floored_to_min_lot": True,
        "graduation_stage_usd": 5,
        "account_balance_at_entry_usd": 50.0,
    },
    "management": {
        "atr_context": None,
        "breakeven": {"applicable": False, "trigger_r": None, "triggered": False, "triggered_at": None},
    },
    "comments": [],
    "adjustments": [],
    "lifecycle": {
        "generated_at": "2026-09-23T07:51:14.973853+00:00",
        "triggered_at": "2026-09-23T07:51:14.973853+00:00",
        "closed_at": None,
    },
    "resolution": {"outcome": None, "exit_price": None, "realized_pnl_usd": None, "realized_r": None},
}


def test_flo_journal_becomes_a_buy_the_ea_can_execute():
    original = deepcopy(FLO)
    command = schema.normalize_command(FLO)
    report = validator.validate_command(command)

    assert FLO == original, "normalization must not mutate the inbox payload"
    assert command["_meta"]["is_journal"] is True
    assert command["action"] == "buy"
    assert command["symbol"] == "GBPCHF"
    assert command["size"]["value"] == 0.02
    assert command["size"]["unit"] == "lots"
    assert command["stop_loss"]["unit"] == "price_distance"
    assert command["stop_loss"]["value"] == round(1.0955599999999999 - 1.09335, 10)
    assert command["take_profit"]["unit"] == "price_distance"
    assert command["take_profit"]["value"] == round(1.0999799999999995 - 1.0955599999999999, 10)
    assert command["price"] == 1.0955599999999999
    assert command["request_id"] == "20260922T195825Z_FLO_GBPCHF_long:buy"
    assert command["strategy_id"] == "Flossbach Liquidation Sweep"
    assert command["breakeven"]["trigger"]["unit"] == "price_distance"
    assert command["breakeven"]["trigger"]["value"] == command["stop_loss"]["value"]
    assert report["valid"], report["errors"]

    lean = public_command(command)
    assert "pair" not in lean
    assert "setup" not in lean
    assert "sizing" not in lean
    assert "management" not in lean
    assert "playbook" not in lean
    assert "_meta" not in lean
    assert lean["action"] == "buy"
    assert lean["symbol"] == "GBPCHF"


def test_g8_short_without_target_omits_take_profit_and_inactive_breakeven():
    command = schema.normalize_command(G8_SHORT)
    report = validator.validate_command(command)
    assert command["action"] == "sell"
    assert command["symbol"] == "GBPUSD"
    assert command["size"]["value"] == 0.01
    assert command["stop_loss"]["value"] == round(1.33873 - 1.33216, 10)
    assert "take_profit" not in command
    assert "breakeven" not in command
    assert report["valid"], report["errors"]


def test_native_commands_are_unchanged():
    command = schema.normalize_command(BUY)
    assert command.get("_meta", {}).get("is_journal") is not True
    assert command["action"] == "buy"
    assert command["symbol"] == "EURUSD"


def test_pending_journal_is_not_a_trade():
    payload = {**FLO, "status": "pending"}
    command = schema.normalize_command(payload)
    assert "error" in command
    assert "pending" in command["error"]


def test_closed_journal_becomes_close_position_with_a_distinct_request_id():
    payload = {**FLO, "status": "closed"}
    command = schema.normalize_command(payload)
    report = validator.validate_command(command)
    assert command["action"] == "close_position"
    assert command["symbol"] == "GBPCHF"
    assert command["request_id"].endswith(":close_position")
    assert "size" not in command
    assert "stop_loss" not in command
    assert report["valid"], report["errors"]


def test_wrong_side_stop_is_refused_not_flipped():
    payload = deepcopy(FLO)
    payload["setup"]["stop"] = 1.20
    command = schema.normalize_command(payload)
    assert "error" in command
    assert "wrong side" in command["error"]


def test_absolute_stop_without_entry_is_refused_rather_than_treated_as_points():
    payload = deepcopy(FLO)
    payload["setup"]["entry"] = None
    command = schema.normalize_command(payload)
    assert "error" in command
    assert "setup.entry" in command["error"]


def test_atr_context_is_used_only_when_there_is_no_stop():
    payload = deepcopy(G8_SHORT)
    payload["setup"]["stop"] = None
    payload["management"]["atr_context"] = {"period": 14, "multiplier": 1.5}
    command = schema.normalize_command(payload)
    assert command["stop_loss"]["unit"] == "atr"
    assert command["stop_loss"]["multiplier"] == 1.5


def test_journal_submit_is_queued_and_raw_payload_keeps_the_full_record(service):
    result = service.submit(fresh(FLO), source="file", source_file="FLO_GBPCHF_long.json")
    assert result["ok"] is True
    assert result["status"] == "queued"
    stored = service.store.get_signal(result["request_id"])
    assert stored["symbol"] == "GBPCHF"
    assert stored["action"] == "buy"
    assert stored["raw_payload"]["pair"] == "GBPCHF"
    assert stored["raw_payload"]["playbook"] == "Flossbach Liquidation Sweep"
    assert "pair" not in stored["command"]
    envelope = service.ea_poll()
    assert envelope["normalized"]["symbol"] == "GBPCHF"
    assert "pair" not in envelope["normalized"]
    assert "setup" not in envelope["normalized"]


def test_a_stale_journal_trigger_is_refused_not_traded(service):
    result = service.submit(fresh(FLO, seconds_ago=7200))
    assert result["ok"] is False
    assert any("journal_max_age_seconds" in e for e in result["errors"])
    assert service.store.queue_depth() == 0


def test_the_age_gate_follows_the_setting(service):
    service.settings.journal_max_age_seconds = 0
    assert service.submit(fresh(FLO, seconds_ago=7200))["ok"] is True


def test_preview_reports_the_stale_refusal_before_anything_is_queued(service):
    report = service.preview(fresh(FLO, seconds_ago=7200))
    assert report["valid"] is False
    assert any("journal_max_age_seconds" in e for e in report["errors"])
    assert service.store.queue_depth() == 0


def test_a_native_command_is_not_subject_to_the_journal_age_gate(service):
    old = dict(BUY)
    old["timestamp"] = "2020-01-01T00:00:00+00:00"
    assert service.submit(old)["ok"] is True


def test_an_in_place_journal_update_cannot_trade_twice(service):
    """The journal app fills in its own record as the trade progresses.

    The request_id is the journal id, and an explicit request_id is an
    idempotency key, so the updated record is a duplicate rather than a second
    order.
    """
    first = fresh(FLO)
    assert service.submit(first)["ok"] is True
    updated = deepcopy(first)
    updated["resolution"]["outcome"] = "win"
    updated["lifecycle"]["closed_at"] = updated["lifecycle"]["triggered_at"]
    second = service.submit(updated)
    assert second["ok"] is False
    assert second["status"] == "duplicate"
    assert service.store.queue_depth() == 1
