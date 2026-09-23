"""Extract Bot_Trader commands from trade-journal JSON.

Producers such as the playbook journal write a research record, not an EA
command: pair/direction, absolute setup prices, nested sizing, lifecycle
status. Bot_Trader understands none of that as-is, and forwarding the whole
object is unsafe because the EA's JSON reader is an unscoped substring scan.

This module recognises that shape, copies only the fields the EA can execute,
and converts absolute stop/target prices into ``points`` distances for better
broker compatibility. The inbox file is never rewritten; the original body
stays in the ledger as ``raw_payload``.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

from . import contract
from .util import as_float

# Status tokens that mean "this record is a live entry to trade".
_ENTRY_TOKENS = (
    "triggered",
    "active",
    "open",
    "live",
    "filled",
    "working",
)

# Status tokens that mean "flatten this symbol" rather than open it.
_CLOSE_TOKENS = (
    "closed",
    "close",
    "stopped",
    "resolved",
    "completed",
    "cancelled",
    "canceled",
    "expired",
    "done",
    "target_hit",
    "stopped_out",
)

# Status tokens that are not a broker instruction.
_SKIP_TOKENS = (
    "pending",
    "wait",
    "watch",
    "idea",
    "setup",
    "draft",
    "candidate",
    "planned",
    "armed",
)

_LONG_SIDES = frozenset({"long", "buy", "open_buy", "openlong"})
_SHORT_SIDES = frozenset({"short", "sell", "open_sell", "openshort"})


def looks_journal(payload: Any) -> bool:
    """True when the object is a journal record rather than a native command.

    A native command already has ``action``. A journal record names the
    instrument as ``pair`` (or ``symbol``) and the side as ``direction``,
    and carries at least one of the journal sections (setup, sizing,
    lifecycle, playbook/book).
    """
    if not isinstance(payload, dict):
        return False
    if payload.get("action"):
        return False
    pair = payload.get("pair") or payload.get("symbol")
    direction = payload.get("direction") or payload.get("side")
    if not pair or not direction:
        return False
    return any(
        isinstance(payload.get(key), dict)
        for key in ("setup", "sizing", "management", "lifecycle", "resolution")
    ) or bool(payload.get("playbook") or payload.get("book") or payload.get("tag"))


def extract_journal_command(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Return a lean EA command, or ``{"error": ...}``.

    The result is deliberately small: unknown journal fields stay in the
    original file and in ``raw_payload``, they are not copied onto the
    command the EA will parse.
    """
    warnings: List[str] = []
    identity = {
        "id": payload.get("id"),
        "book": payload.get("book"),
        "tag": payload.get("tag"),
        "playbook": payload.get("playbook"),
        "tier": payload.get("tier"),
        "status": payload.get("status"),
    }

    symbol = str(payload.get("pair") or payload.get("symbol") or "").strip()
    if not symbol:
        return {"error": "journal record has no pair/symbol"}

    status_text = str(payload.get("status") or "").strip().lower()
    kind = _status_kind(status_text)
    if kind == "skip":
        return {
            "error": (
                f"journal record status '{payload.get('status')}' is not an "
                "executable trade"
            )
        }

    if kind == "close":
        command: Dict[str, Any] = {
            "action": "close_position",
            "symbol": symbol,
        }
        _stamp_ids(command, payload, "close_position")
        _stamp_strategy(command, payload)
        return _with_notes(command, warnings, identity)

    side = str(payload.get("direction") or payload.get("side") or "").strip().lower()
    if side in _LONG_SIDES:
        action = "buy"
        is_long = True
    elif side in _SHORT_SIDES:
        action = "sell"
        is_long = False
    else:
        return {"error": f"journal direction '{payload.get('direction')}' is not long or short"}

    if kind is None and status_text:
        warnings.append(
            f"journal status '{payload.get('status')}' is not a known lifecycle "
            "token; treating the record as an entry"
        )

    setup = payload.get("setup") if isinstance(payload.get("setup"), dict) else {}
    sizing = payload.get("sizing") if isinstance(payload.get("sizing"), dict) else {}
    management = payload.get("management") if isinstance(payload.get("management"), dict) else {}

    entry = as_float(setup.get("entry"))
    stop = as_float(setup.get("stop"))
    target = as_float(setup.get("target"))

    command = {
        "action": action,
        "symbol": symbol,
    }
    _stamp_ids(command, payload, action)
    _stamp_strategy(command, payload)

    lots = as_float(sizing.get("lots", sizing.get("lot")))
    if lots is not None:
        command["size"] = {"type": "fixed", "unit": "lots", "value": lots}

    # Use the pre-calculated pip_distance from sizing if available - this is more accurate
    # than calculating from absolute prices since the signal source already did the math
    pip_distance = as_float(sizing.get("pip_distance"))
    
    if pip_distance is not None and stop is not None:
        # Convert pips to points (1 pip = 10 points for most forex pairs)
        # This is more reliable than price_distance conversion
        command["stop_loss"] = {"type": "distance", "unit": "points", "value": round(pip_distance * 10, 1)}
    else:
        # Fallback to absolute price conversion if pip_distance not available
        stop_loss, sl_error = _absolute_to_gap(entry, stop, is_long, is_stop=True, name="stop", symbol=symbol, safety_margin=0.0)
        if sl_error:
            return {"error": sl_error}
        if stop_loss is not None:
            command["stop_loss"] = stop_loss
        else:
            atr_sl, atr_warning = _atr_stop(management.get("atr_context"))
            if atr_sl is not None:
                command["stop_loss"] = atr_sl
            if atr_warning:
                warnings.append(atr_warning)

    # Take profit: only use absolute price conversion (no pip_distance field for TP)
    take_profit, tp_error = _absolute_to_gap(entry, target, is_long, is_stop=False, name="target", symbol=symbol, safety_margin=0.0)
    if tp_error:
        return {"error": tp_error}
    if take_profit is not None:
        command["take_profit"] = take_profit

    if entry is not None and entry > 0:
        command["price"] = entry
        if pip_distance is not None:
            warnings.append(
                "journal setup.entry is recorded as price; EA v6.5 ignores price and "
                "executes entries at market. stop_loss uses sizing.pip_distance converted "
                "to points; take_profit is converted from planned entry gap"
            )
        else:
            warnings.append(
                "journal setup.entry is recorded as price; EA v6.5 ignores price and "
                "executes entries at market. stop_loss/take_profit are converted to "
                "price_distance from the planned entry gap and applied from the fill"
            )

    be_block, be_warnings = _breakeven(management.get("breakeven"), command.get("stop_loss"))
    warnings.extend(be_warnings)
    if be_block is not None:
        command["breakeven"] = be_block

    if management.get("atr_context") is not None and "stop_loss" in command:
        if command["stop_loss"].get("unit") != "atr":
            sl_unit = command["stop_loss"].get("unit")
            if sl_unit == "points":
                warnings.append("journal management.atr_context is ignored because setup.stop is present and converted to points from sizing.pip_distance")
            else:
                warnings.append("journal management.atr_context is ignored because setup.stop is present and converted to price_distance")

    return _with_notes(command, warnings, identity)


def _status_kind(status: str) -> Optional[str]:
    if not status:
        return "entry"
    if any(token in status for token in _SKIP_TOKENS):
        return "skip"
    if any(token in status for token in _CLOSE_TOKENS) and "triggered" not in status:
        return "close"
    if any(token in status for token in _ENTRY_TOKENS):
        return "entry"
    return None


def _stamp_ids(command: Dict[str, Any], payload: Dict[str, Any], action: str) -> None:
    journal_id = payload.get("id")
    if journal_id:
        command["request_id"] = f"{journal_id}:{action}"
    lifecycle = payload.get("lifecycle") if isinstance(payload.get("lifecycle"), dict) else {}
    stamp = lifecycle.get("triggered_at") or lifecycle.get("generated_at") or payload.get("timestamp")
    if stamp:
        command["timestamp"] = stamp


def _stamp_strategy(command: Dict[str, Any], payload: Dict[str, Any]) -> None:
    strategy = payload.get("playbook") or payload.get("tag") or payload.get("book")
    if strategy:
        command["strategy_id"] = str(strategy)


def _absolute_to_gap(
    entry: Optional[float],
    level: Optional[float],
    is_long: bool,
    *,
    is_stop: bool,
    name: str,
    symbol: Optional[str] = None,
    safety_margin: float = 0.0,
) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """Turn an absolute journal price into an EA distance object.

    The EA never takes an SL/TP as an absolute price: it converts a gap into
    points and applies that gap from the fill. Using the planned entry as the
    origin keeps the journal's R-multiple instead of silently treating
    ``1.09335`` as a 1.09335-price gap (hundreds of thousands of points).

    We use 'price_distance' which the EA converts to points using SYMBOL_POINT.
    A safety_margin can be added to help meet broker minimum distance requirements.
    """
    if level is None:
        return None, None
    if level <= 0:
        return None, f"journal setup.{name} must be a price greater than zero"
    if entry is None or entry <= 0:
        return None, (
            f"journal setup.{name} is an absolute price; setup.entry is required "
            "to convert it into a distance the EA can apply"
        )
    if is_stop:
        gap = (entry - level) if is_long else (level - entry)
        side = "below" if is_long else "above"
    else:
        gap = (level - entry) if is_long else (entry - level)
        side = "above" if is_long else "below"
    if gap <= 0:
        return None, (
            f"journal setup.{name} {level} is on the wrong side of entry {entry} "
            f"for this direction (expected {side} entry)"
        )
    
    # Apply safety margin to help meet broker minimum distance requirements
    adjusted_gap = gap * (1.0 + safety_margin) if safety_margin > 0 else gap
    
    # Use price_distance - let the EA handle the conversion to points
    return {"type": "distance", "unit": "price_distance", "value": round(adjusted_gap, 10)}, None


def _atr_stop(atr_context: Any) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    if atr_context is None:
        return None, None
    if isinstance(atr_context, bool):
        return None, "journal atr_context is not a usable ATR spec"
    numeric = as_float(atr_context)
    if numeric is not None:
        if numeric <= 0:
            return None, "journal atr_context must be greater than zero"
        return {
            "type": "distance",
            "unit": "atr",
            "period": contract.DEFAULT_ATR_PERIOD,
            "timeframe": contract.DEFAULT_ATR_TIMEFRAME,
            "multiplier": numeric,
        }, None
    if not isinstance(atr_context, dict):
        return None, None
    if any(key in atr_context for key in ("multiplier", "period", "timeframe")):
        spec = {
            "type": "distance",
            "unit": "atr",
            "period": atr_context.get("period", contract.DEFAULT_ATR_PERIOD),
            "timeframe": atr_context.get("timeframe", contract.DEFAULT_ATR_TIMEFRAME),
            "multiplier": atr_context.get("multiplier", contract.DEFAULT_ATR_MULTIPLIER),
        }
        return spec, None
    value = as_float(atr_context.get("value") or atr_context.get("atr"))
    if value is not None and value > 0:
        return {"type": "distance", "unit": "price_distance", "value": round(value, 10)}, None
    return None, None


def _breakeven(
    spec: Any, stop_loss: Optional[Dict[str, Any]]
) -> Tuple[Optional[Dict[str, Any]], List[str]]:
    warnings: List[str] = []
    if not isinstance(spec, dict):
        return None, warnings
    if spec.get("applicable") is False:
        return None, warnings
    if spec.get("applicable") is not True and spec.get("trigger_r") is None and spec.get("lock_in") is None:
        return None, warnings

    block: Dict[str, Any] = {}
    trigger_r = as_float(spec.get("trigger_r"))
    sl_value = as_float((stop_loss or {}).get("value"))
    sl_unit = (stop_loss or {}).get("unit")
    if trigger_r is not None and trigger_r > 0 and sl_value is not None and sl_unit in ("price_distance", "points"):
        # Support both price_distance and points for stop_loss units
        unit = sl_unit  # Keep the same unit as the stop_loss
        block["trigger"] = {
            "type": "distance",
            "unit": unit,
            "value": round(sl_value * trigger_r, 10 if unit == "price_distance" else 1),
        }
    elif trigger_r is not None:
        warnings.append(
            "journal breakeven.trigger_r could not be converted (needs a "
            "price_distance or points stop_loss); it is omitted rather than guessed"
        )

    lock_in = spec.get("lock_in") or spec.get("lock")
    if lock_in is not None:
        numeric = as_float(lock_in)
        if numeric is not None:
            block["lock_in"] = {"type": "distance", "unit": "points", "value": numeric}
        elif isinstance(lock_in, dict):
            block["lock_in"] = lock_in

    if not block:
        return None, warnings

    warnings.append(
        "journal breakeven is attached to the entry command for the board; "
        "EA v6.5 does not auto-move the stop after entry - it only honours a "
        "separate breakeven action, and then immediately"
    )
    return block, warnings


def _with_notes(
    command: Dict[str, Any], warnings: List[str], identity: Dict[str, Any]
) -> Dict[str, Any]:
    command["_journal"] = {
        "warnings": warnings,
        "identity": {k: v for k, v in identity.items() if v is not None},
    }
    return command
