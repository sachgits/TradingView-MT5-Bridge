"""Command normalization: every accepted input shape -> one canonical command.

Design rules, in order of importance:

1. **Never invent a trade parameter.** A missing size, stop or symbol stays
   missing. The validator decides whether that is acceptable; the EA decides
   what default to apply. Guessing here is how a bridge ends up placing an
   order nobody asked for.
2. **Preserve unknown fields.** The output starts as a copy of the input, so a
   field the EA learns to read next month already reaches it without a bridge
   release. Unknown keys are reported, not dropped.
3. **Canonicalize what we understand.** Action aliases fold to the EA's
   canonical names and sizes/distances become uniform objects, while the
   sender's original spelling is kept alongside for the audit trail.

The EA parses JSON with a naive substring scan (JsonGetString/JsonGetObject in
Bot_Trader.mq5), which cannot distinguish a top-level key from a nested one.
That constrains what is safe to emit - see the note on `_SHADOWING_KEYS`.
"""

from __future__ import annotations

import json
from copy import deepcopy
from typing import Any, Dict, List

from . import contract
from .journal import extract_journal_command, looks_journal
from .legacy import looks_legacy, parse_legacy_signal
from .util import as_float

# Keys the EA looks up with an unscoped substring search over the whole command
# object. If one of these appears only inside a nested object, the EA can still
# match it and read the wrong value. Normalization promotes them to the top
# level where the sender used a known alias, and warns where it cannot.
#
# "value" is deliberately absent: Bot_Trader v6.5 no longer accepts a bare
# top-level "value" as a size, precisely because it could not be told apart from
# a nested stop_loss.value. "unit" and "type" are only ever read inside the
# size/distance object the EA has already brace-matched, so they are scoped.
_SHADOWING_KEYS = frozenset({"action", "symbol", "size", "lot"})

# Sender aliases folded into the canonical field name.
_SIZE_ALIASES = ("size", "lot_size", "lots", "lot", "volume")
_SL_ALIASES = ("stop_loss", "sl", "sl_distance", "stoploss")
_TP_ALIASES = ("take_profit", "tp", "tp_distance", "takeprofit")

# Fields carried through untouched when present. Everything else is preserved
# too (rule 2); this list only names what the bridge actively understands and
# validates.
_KNOWN_PASSTHROUGH = (
    "request_id",
    "timestamp",
    "strategy_id",
    "magic",
    "execution_mode",
    "risk_percent",
    "close_percent",
    "breakeven",
    "trailing",
    "take_profits",
    "price",
    "scope",
    "position_ticket",
    "reason",
    "expiration",
    "comment",
    "client",
    # EA v6.5 pending-order engine fields.
    "order_type",
    "price_unit",
    "stoplimit",
    "order_ticket",
    # EA v6.6 additions.
    "close_volume",
    "deviation",
)


def _first_present(payload: Dict[str, Any], names: tuple) -> tuple:
    """Return (name, value) for the first alias the sender actually supplied."""
    for name in names:
        if name in payload and payload[name] is not None:
            return name, payload[name]
    return None, None


def _coerce_numeric(value: Any) -> Any:
    """Turn a numeric string into a number, leaving anything else alone.

    ``{"stop_loss": "200"}`` and ``{"stop_loss": 200}`` are the same command;
    coercing here keeps the canonical form stable so dedup hashing and the
    dashboard do not have to cope with both spellings. Non-numeric values are
    preserved untouched so the validator can report them precisely.
    """
    number = as_float(value)
    return number if number is not None else value


def _fold_alias(result: Dict[str, Any], canonical: str, aliases: tuple, *, integer: bool = False) -> None:
    """Promote the first present alias onto ``canonical`` and drop the rest.

    The EA reads these keys with an unscoped substring scan and tries its own
    aliases in order, so leaving two spellings in the body risks it picking the
    stale one. Folding to a single canonical key mirrors what normalization
    already does for size and SL/TP.
    """
    name, value = _first_present(result, aliases)
    if name is None:
        return
    if integer:
        number = as_float(value)
        result[canonical] = int(number) if number is not None else value
    else:
        result[canonical] = _coerce_numeric(value)
    for alias in aliases:
        if alias != canonical:
            result.pop(alias, None)


def normalize_size(value: Any) -> Dict[str, Any]:
    """Coerce a size into {"type","unit","value"} without choosing a default."""
    if isinstance(value, dict):
        result = deepcopy(value)
        result.setdefault("type", "fixed")
        unit = str(result.get("unit") or "lots").strip().lower()
        result["unit"] = contract.SIZE_UNIT_ALIASES.get(unit, unit)
        if "value" not in result and "amount" in result:
            result["value"] = result["amount"]
        if "value" in result:
            result["value"] = _coerce_numeric(result["value"])
        return result
    return {"type": "fixed", "unit": "lots", "value": _coerce_numeric(value)}


def normalize_distance(value: Any) -> Dict[str, Any]:
    """Coerce an SL/TP into {"type","unit","value"} without choosing a default."""
    if isinstance(value, dict):
        result = deepcopy(value)
        result.setdefault("type", "distance")
        unit = str(result.get("unit") or result.get("type") or "points").strip().lower()
        result["unit"] = unit
        if "value" not in result and "distance" in result:
            result["value"] = result["distance"]
        if "value" in result:
            result["value"] = _coerce_numeric(result["value"])
        if unit == "atr":
            result.setdefault("period", contract.DEFAULT_ATR_PERIOD)
            result.setdefault("timeframe", contract.DEFAULT_ATR_TIMEFRAME)
            result.setdefault("multiplier", contract.DEFAULT_ATR_MULTIPLIER)
            for key in ("period", "multiplier"):
                result[key] = _coerce_numeric(result[key])
        return result
    # A bare number or numeric string is a distance in points, matching the
    # EA's ParseDistanceSpec() fallback.
    return {"type": "distance", "unit": "points", "value": _coerce_numeric(value)}


def _normalize_payload(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Canonicalize a structured command, preserving unknown fields."""
    result: Dict[str, Any] = deepcopy(payload)
    warnings: List[str] = []

    result["schema_version"] = str(payload.get("schema_version") or contract.SCHEMA_VERSION)

    raw_action = payload.get("action")
    canonical = contract.normalize_action(raw_action)
    if raw_action is not None:
        result["action"] = canonical
        if canonical != str(raw_action).strip().lower():
            result["action_raw"] = raw_action

    # --- size -------------------------------------------------------------
    name, raw_size = _first_present(payload, _SIZE_ALIASES)
    if name is not None:
        result["size"] = normalize_size(raw_size)
        if name != "size":
            result["size_alias"] = name
            # Drop the alias so the EA cannot read a stale second copy.
            result.pop(name, None)

    # --- stop loss / take profit ------------------------------------------
    for canonical_key, aliases in (("stop_loss", _SL_ALIASES), ("take_profit", _TP_ALIASES)):
        name, raw = _first_present(payload, aliases)
        if name is None:
            continue
        result[canonical_key] = normalize_distance(raw)
        if name != canonical_key:
            result[canonical_key + "_alias"] = name
            result.pop(name, None)

    # --- breakeven / trailing sub-distances --------------------------------
    # The EA reads these with ParseDistanceSpecObject(), which looks for a
    # nested OBJECT and otherwise falls back to the parent breakeven/trailing
    # object itself. A bare number is therefore silently discarded and replaced
    # by the EA's own default (20 points for lock_in, 100 for trigger, 60 for
    # trailing distance) - so {"lock_in": 5} would trade as 20. Canonicalizing
    # the number into a distance object is what makes the sender's intent reach
    # the terminal.
    for parent, keys in (("breakeven", ("trigger", "lock_in")), ("trailing", ("activation", "distance"))):
        block = result.get(parent)
        if not isinstance(block, dict):
            continue
        for key in keys:
            if key in block:
                block[key] = normalize_distance(block[key])

    # --- pending-order & position management fields (EA v6.5 / v6.6) --------
    # Fold each sender alias onto the canonical key the EA reads, drop the
    # alias, and canonicalize order_type/price_unit so validation and dedup see
    # one stable spelling. Only touches keys the sender actually supplied - a
    # market order carries none of these and gains none.
    _fold_alias(result, "price", contract.PRICE_ALIASES)
    _fold_alias(result, "stoplimit", contract.STOPLIMIT_ALIASES)
    if canonical in (contract.MANAGEMENT_ACTIONS | {"close_position"}):
        _fold_alias(result, "position_ticket", ("position_ticket", "ticket"), integer=True)
        _fold_alias(result, "order_ticket", ("order_ticket",), integer=True)
    else:
        _fold_alias(result, "order_ticket", contract.ORDER_TICKET_ALIASES, integer=True)
        _fold_alias(result, "position_ticket", ("position_ticket",), integer=True)
    _fold_alias(result, "deviation", ("deviation", "slippage"), integer=True)
    _fold_alias(result, "expiration", contract.EXPIRATION_ALIASES)
    if "close_volume" in result:
        result["close_volume"] = _coerce_numeric(result["close_volume"])
    if result.get("order_type") is not None:
        result["order_type"] = contract.normalize_action(result["order_type"])
    if result.get("price_unit") is not None:
        result["price_unit"] = str(result["price_unit"]).strip().lower()

    # --- unknown fields ----------------------------------------------------
    # Semantic problems (an unresolvable unit, an approximate size mode, a
    # feature the EA parses but ignores) are the validator's job. Normalization
    # only reports structure, so a problem is never stated twice.
    known = set(_KNOWN_PASSTHROUGH) | {
        "schema_version", "action", "action_raw", "symbol", "size", "size_alias",
        "stop_loss", "stop_loss_alias", "take_profit", "take_profit_alias",
        "legacy_signal", "defaults_from_ea", "_meta",
    }
    unknown = sorted(k for k in result if k not in known)
    if unknown:
        warnings.append("fields not in the bridge contract are forwarded as-is: " + ", ".join(unknown))

    result["_meta"] = {
        "is_legacy": False,
        "warnings": warnings,
        "unknown_fields": unknown,
    }
    return result


def normalize_command(payload: Any) -> Dict[str, Any]:
    """Normalize any accepted input into a canonical command.

    Returns ``{"error": ...}`` when the payload is not a command at all.
    Semantic problems (a bad unit, a missing size) are left for the validator:
    normalization reports shape, validation reports safety.
    """
    if isinstance(payload, (bytes, bytearray)):
        try:
            payload = payload.decode("utf-8")
        except UnicodeDecodeError:
            return {"error": "payload is not valid UTF-8"}

    if isinstance(payload, str):
        text = payload.strip()
        if not text:
            return {"error": "empty payload"}
        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            # A body that opens like JSON but does not parse is a truncated
            # command, not legacy text: '{"action": "buy", "sym' still contains
            # the word buy, and the legacy parser would turn that into a market
            # order sized by the EA's own inputs.
            if text[:1] in ("{", "["):
                return {"error": "malformed or truncated JSON"}
            return _from_legacy(text)

    if not isinstance(payload, dict):
        return {"error": "payload must be a JSON object or a legacy signal string"}

    # Trade-journal records (pair/direction/setup) are not EA commands. Extract
    # only the fields Bot_Trader can execute; the original object is left in
    # the inbox and stored as raw_payload.
    if looks_journal(payload):
        extracted = extract_journal_command(payload)
        if "error" in extracted:
            return extracted
        notes = extracted.pop("_journal", {}) or {}
        result = _normalize_payload(extracted)
        meta = result.setdefault("_meta", {"is_legacy": False, "warnings": [], "unknown_fields": []})
        meta["is_journal"] = True
        meta["journal"] = notes.get("identity") or {}
        extra = list(notes.get("warnings") or [])
        meta["warnings"] = extra + list(meta.get("warnings") or [])
        return result

    # The extension and other v1 producers post {"signal": "BUY SL=50 ..."}.
    signal = payload.get("signal")
    if signal is not None and looks_legacy(signal):
        command = _from_legacy(str(signal))
        if "error" in command:
            return command
        # Anything else in the envelope (request_id, timestamp, client) still
        # applies to the command it carried.
        for key, value in payload.items():
            if key != "signal":
                command.setdefault(key, value)
        command.setdefault("_meta", {"is_legacy": True, "warnings": [], "unknown_fields": []})
        command["_meta"]["is_legacy"] = True
        return command

    if signal is not None and isinstance(signal, str) and not looks_legacy(signal):
        # A JSON-encoded command nested under "signal".
        try:
            nested = json.loads(signal)
        except json.JSONDecodeError:
            return {"error": "'signal' is neither legacy text nor valid JSON"}
        if isinstance(nested, dict):
            merged = dict(nested)
            for key, value in payload.items():
                if key != "signal":
                    merged.setdefault(key, value)
            return _normalize_payload(merged)

    return _normalize_payload(payload)


def _from_legacy(text: str) -> Dict[str, Any]:
    parsed = parse_legacy_signal(text)
    if "error" in parsed:
        return parsed
    parsed["_meta"] = {
        "is_legacy": True,
        "warnings": [
            "legacy plain-text signal: symbol comes from the EA's chart, and any "
            "omitted SL/TP/LOT comes from the EA's inputs"
        ],
        "unknown_fields": [],
    }
    return parsed


def summarize(command: Dict[str, Any]) -> str:
    """One-line description for logs and the event feed."""
    action = command.get("action", "?")
    symbol = command.get("symbol") or "<chart symbol>"
    size = command.get("size")
    if isinstance(size, dict):
        return f"{action} {symbol} {size.get('value')} {size.get('unit', 'lots')}"
    if size is not None:
        return f"{action} {symbol} {size}"
    return f"{action} {symbol}"


def shadowing_risk(command: Dict[str, Any]) -> List[str]:
    """Flag nested keys the EA's unscoped parser could mis-read.

    The EA finds the first textual occurrence of a key anywhere in the JSON, so
    a nested ``"value"`` can be read as a top-level one. This is reported on
    the signal board rather than silently rewritten, because the bridge cannot
    know which occurrence the sender meant.
    """
    risks: List[str] = []
    for key, value in command.items():
        if key.startswith("_") or not isinstance(value, dict):
            continue
        for nested in value:
            if nested in _SHADOWING_KEYS and nested not in command:
                risks.append(
                    f"'{key}.{nested}' may be mis-read by the EA as a top-level '{nested}'"
                )
    return risks
