"""Normalization and validation: shape first, safety second."""
import pytest

from bridge import contract, schema, validator

from conftest import BUY


def normalize(payload):
    return schema.normalize_command(payload)


def check(payload, mode=validator.MODE_STRICT):
    command = normalize(payload)
    assert "error" not in command, command
    return command, validator.validate_command(command, mode)


def test_legacy_plain_text_is_flagged_as_legacy():
    command = normalize("BUY SL=50 TP=100 LOT=0.01")
    assert command["_meta"]["is_legacy"] is True
    assert command["action"] == "buy"
    assert any("legacy" in w for w in command["_meta"]["warnings"])


def test_legacy_envelope_from_the_extension():
    command, report = check({"signal": "BUY SL=50 TP=100 LOT=0.01"})
    assert report["valid"], report["errors"]
    assert command["_meta"]["is_legacy"] is True


def test_truncated_json_is_never_read_as_legacy_text():
    # '{"action": "buy", "sym' contains the word buy; the legacy parser would
    # have turned a corrupt body into a live market order.
    command = normalize('{"action": "buy", "symbol": "EURUSD", "size":')
    assert "error" in command
    assert "legacy" not in str(command.get("_meta", {}))


def test_missing_size_is_refused_in_strict_but_warned_in_permissive():
    _, strict = check({"action": "buy", "symbol": "EURUSD"})
    assert not strict["valid"]
    assert any("size" in e for e in strict["errors"])

    _, permissive = check({"action": "buy", "symbol": "EURUSD"}, validator.MODE_PERMISSIVE)
    assert permissive["valid"]
    assert any("size" in w for w in permissive["warnings"])


def test_size_alias_is_canonicalized_and_the_alias_dropped():
    command, report = check({"action": "buy", "symbol": "EURUSD", "lot": 0.2})
    assert report["valid"], report["errors"]
    assert command["size"] == {"type": "fixed", "unit": "lots", "value": 0.2}
    assert command["size_alias"] == "lot"
    assert "lot" not in command


def test_bare_number_distance_becomes_points():
    command, report = check({**BUY, "stop_loss": 250})
    assert report["valid"], report["errors"]
    assert command["stop_loss"] == {"type": "distance", "unit": "points", "value": 250}


def test_atr_distance_gets_documented_defaults():
    command, _ = check({**BUY, "stop_loss": {"unit": "atr", "multiplier": 2}})
    assert command["stop_loss"]["period"] == contract.DEFAULT_ATR_PERIOD
    assert command["stop_loss"]["multiplier"] == 2


def test_breakeven_sub_distances_are_canonicalized():
    # The EA discards a bare number here and substitutes its own 20 points.
    command, report = check({"action": "breakeven", "symbol": "EURUSD",
                             "breakeven": {"lock_in": 25}})
    assert report["valid"], report["errors"]
    assert command["breakeven"]["lock_in"] == {"type": "distance", "unit": "points", "value": 25}


def test_trailing_sub_distances_are_canonicalized():
    command, _ = check({"action": "breakeven", "symbol": "EURUSD",
                        "trailing": {"distance": 60, "activation": 10}})
    assert command["trailing"]["distance"]["unit"] == "points"
    assert command["trailing"]["activation"]["value"] == 10


def test_unknown_fields_survive_and_are_listed():
    command, report = check({**BUY, "client_note": "news trade"})
    assert command["client_note"] == "news trade"
    assert "client_note" in command["_meta"]["unknown_fields"]
    assert any("client_note" in w for w in report["warnings"])


def test_shadowing_risk_is_silent_for_the_canonical_vocabulary():
    command, _ = check(BUY)
    assert schema.shadowing_risk(command) == []


def test_shadowing_risk_flags_a_nested_reserved_key():
    # With no top-level symbol, the EA's first textual match for "symbol" is
    # the nested one - exactly the mis-read the warning exists for.
    command = normalize({"action": "buy", "weird": {"symbol": "XAUUSD"}})
    assert len(schema.shadowing_risk(command)) == 1


@pytest.mark.parametrize("action", ["buy", "sell"])
def test_entry_actions_validate(action):
    _, report = check({**BUY, "action": action})
    assert report["valid"], report["errors"]


def test_modify_needs_a_target():
    _, report = check({"action": "modify", "symbol": "EURUSD"})
    assert not report["valid"]
    _, ok = check({"action": "modify", "symbol": "EURUSD", "stop_loss": 100})
    assert ok["valid"], ok["errors"]


def test_close_position_needs_symbol_or_ticket():
    _, report = check({"action": "close_position"})
    assert not report["valid"]
    _, ok = check({"action": "close_position", "symbol": "EURUSD"})
    assert ok["valid"], ok["errors"]


def test_partial_close_percent_bounds():
    _, zero = check({"action": "partial_close", "symbol": "EURUSD", "close_percent": 0})
    assert not zero["valid"]
    _, over = check({"action": "partial_close", "symbol": "EURUSD", "close_percent": 101})
    assert not over["valid"]
    _, ok = check({"action": "partial_close", "symbol": "EURUSD", "close_percent": 50})
    assert ok["valid"], ok["errors"]


def test_risk_percent_and_magic_bounds():
    _, report = check({**BUY, "risk_percent": 0})
    assert not report["valid"]
    _, report = check({**BUY, "magic": "abc"})
    assert not report["valid"]
    _, ok = check({**BUY, "magic": 7, "risk_percent": 1})
    assert ok["valid"], ok["errors"]


@pytest.mark.parametrize("action", sorted(contract.SKIPPED_ACTIONS))
def test_actions_the_ea_cannot_execute_are_refused(action):
    _, report = check({"action": action, "symbol": "EURUSD"})
    assert not report["valid"]
    assert any("SKIPPED" in e or "not implemented" in e for e in report["errors"])


def test_an_unknown_verb_is_refused():
    _, report = check({"action": "teleport", "symbol": "EURUSD"})
    assert not report["valid"]
    assert any("unsupported action" in e for e in report["errors"])


def test_a_malformed_take_profit_ladder_warns_but_does_not_block():
    # The EA ignores take_profits entirely, so it must never veto a valid entry.
    _, report = check({**BUY, "take_profits": [{"close_percent": 150}]})
    assert report["valid"], report["errors"]
    assert report["warnings"]


def test_negative_distance_is_refused():
    _, report = check({**BUY, "stop_loss": -10})
    assert not report["valid"]
