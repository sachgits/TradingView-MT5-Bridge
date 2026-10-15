"""Validation against the EA's real capabilities.

The rule this module exists to enforce: **the bridge must never accept a
command the EA cannot execute correctly, and never reject one it can.** Both
directions are silent-failure modes - the first loses a trade, the second
loses a feature.

Two modes, selectable at runtime from the signal board:

``strict`` (default)
    An entry must state its own symbol and size. Relying on the EA's
    ``LotSize``/chart-symbol defaults is rejected, because a signal meant for
    one instrument would otherwise be traded on whatever chart the EA happens
    to be attached to.

``permissive``
    Mirrors raw Bot_Trader behaviour: omitted fields fall back to the EA's
    inputs. Useful for single-symbol, single-chart setups and for reproducing
    exactly what a hand-written JSON file would do.

Problems are split into ``errors`` (reject the command) and ``warnings``
(dispatch it, but show the operator what will not happen). A field the EA
parses and ignores is a warning, not an error - the trade is still correct,
just less complete than the sender may believe.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from . import contract
from .util import as_float

MODE_STRICT = "strict"
MODE_PERMISSIVE = "permissive"
VALID_MODES = (MODE_STRICT, MODE_PERMISSIVE)

# Fields that need a symbol to target the right instrument. close_all, noop and
# account are account-wide, inert or read-only, so they are exempt. The pending
# entry types place an order on a specific symbol, so they require one; the
# pending management actions (cancel/modify) may target an order_ticket instead
# and are checked separately.
_SYMBOL_REQUIRED_FOR = (
    contract.ENTRY_ACTIONS
    | contract.MANAGEMENT_ACTIONS
    | contract.PENDING_ACTIONS
    | {"close_symbol", "close_position"}
)


class Report:
    """Accumulates errors and warnings for one command."""

    def __init__(self) -> None:
        self.errors: List[str] = []
        self.warnings: List[str] = []

    def error(self, message: str) -> None:
        self.errors.append(message)

    def warn(self, message: str) -> None:
        self.warnings.append(message)

    @property
    def valid(self) -> bool:
        return not self.errors

    def as_dict(self) -> Dict[str, Any]:
        return {"valid": self.valid, "errors": self.errors, "warnings": self.warnings}


def _check_distance(spec: Any, name: str, report: Report, required: bool = False) -> None:
    if spec is None:
        if required:
            report.error(f"{name} is required")
        return

    if isinstance(spec, bool):
        report.error(f"{name} must be a distance object, not a boolean")
        return
    if isinstance(spec, (int, float)):
        # Normalization turns a bare number into a points distance, so reaching
        # here means it came from a field the bridge does not canonicalize.
        if spec <= 0:
            report.error(f"{name} must be greater than zero")
        return

    if not isinstance(spec, dict):
        report.error(f"{name} must be a distance object such as "
                     '{"unit": "points", "value": 200}')
        return

    unit = str(spec.get("unit", spec.get("type", "points"))).strip().lower()

    if unit in contract.UNSUPPORTED_DISTANCE_UNITS:
        # Not a style complaint: the EA silently substitutes 50 points.
        report.error(f"{name}.unit '{unit}' is not resolvable by the EA - {contract.UNSUPPORTED_DISTANCE_UNITS[unit]}")
        return

    if unit not in contract.DISTANCE_UNITS:
        report.error(
            f"{name}.unit '{unit}' is unknown; the EA resolves "
            + ", ".join(sorted(contract.DISTANCE_UNITS))
        )
        return

    if unit == "atr":
        period = as_float(spec.get("period", contract.DEFAULT_ATR_PERIOD))
        multiplier = as_float(spec.get("multiplier", contract.DEFAULT_ATR_MULTIPLIER))
        if period is None or period < 1:
            report.error(f"{name}.period must be a number >= 1")
        if multiplier is None or multiplier <= 0:
            report.error(f"{name}.multiplier must be greater than zero")
        if spec.get("timeframe") and not contract.ATR_TIMEFRAME_HONOURED:
            report.warn(f"{name}.timeframe is ignored by the EA; ATR always uses M15")
        return

    value = as_float(spec.get("value"))
    if value is None:
        report.error(f"{name}.value is required and must be numeric")
    elif value <= 0:
        report.error(f"{name}.value must be greater than zero")


def _check_size(command: Dict[str, Any], report: Report, strict: bool, is_legacy: bool = False) -> None:
    size = command.get("size")

    if size is None:
        # A legacy signal with no LOT= token is intentionally deferring to the
        # EA's LotSize input - that is the documented v1 behaviour, not an
        # incomplete command.
        if strict and not is_legacy:
            report.error(
                "size is required in strict mode - the bridge will not let the EA's "
                "LotSize input decide a live order's volume"
            )
        else:
            report.warn("size omitted; the EA's LotSize input will be used")
        return

    if not isinstance(size, dict):
        report.error("size must be an object or a number")
        return

    unit = str(size.get("unit", "lots")).strip().lower()
    if unit not in contract.SIZE_UNITS:
        report.error(
            f"size.unit '{unit}' is unknown; the EA supports "
            + ", ".join(sorted(contract.SIZE_UNITS))
        )
        return

    value = as_float(size.get("value"))
    if value is None:
        report.error("size.value is required and must be numeric")
        return

    if unit == "risk":
        # A risk-mode size is a percentage, and 0 is meaningless rather than
        # merely small; other units must simply be positive.
        if value <= 0:
            report.error("size.value must be greater than zero in risk mode")
    elif value <= 0:
        report.error("size.value must be greater than zero")

    if unit == "risk":
        risk_percent = as_float(command.get("risk_percent"))
        if risk_percent is None:
            if strict:
                report.error("risk_percent is required when size.unit is 'risk'")
            else:
                report.warn("risk_percent omitted; the EA's DefaultRiskPercent will be used")
        elif not 0 < risk_percent <= 100:
            report.error("risk_percent must be greater than 0 and no more than 100")

    if unit in ("risk_money", "risk_cash", "risk_amount", "risk_usd"):
        if value <= 0:
            report.error(f"size.value must be greater than zero when size.unit is '{unit}'")
        if command.get("stop_loss") is None:
            if strict:
                report.error(
                    f"stop_loss is required in strict mode when size.unit is '{unit}' "
                    "so the EA can calculate lot size from risk money"
                )
            else:
                report.warn(
                    f"stop_loss omitted with size.unit '{unit}'; the EA's LotSize input will be used"
                )

    if unit in contract.APPROXIMATE_SIZE_UNITS:
        report.warn(
            f"size.unit '{unit}' is a documented approximation in the EA, not exact risk math"
        )


def _check_take_profits(levels: Any, report: Report) -> None:
    """Validate a take-profit ladder, as warnings only.

    The EA parses ``take_profits`` but does not execute a ladder - it places the
    single ``take_profit``. Nothing in this field can change what gets traded,
    so a malformed ladder must not stop an otherwise valid entry: refusing the
    command would be a bigger error than ignoring the field. Everything found
    here is therefore reported as a warning and the trade still goes through.
    """
    report.warn(
        "take_profits is parsed but not executed by EA v6.5; only a single "
        "take_profit is placed. The ladder will be ignored."
    )

    probe = Report()
    if not isinstance(levels, list) or not levels:
        probe.error("take_profits must be a non-empty array")
    else:
        total = 0.0
        previous = 0.0
        for index, level in enumerate(levels, 1):
            if not isinstance(level, dict):
                probe.error(f"take_profits[{index}] must be an object")
                continue
            # Both shapes are accepted: a nested "distance" object, or unit and
            # value written directly on the level.
            distance = level.get("distance")
            if distance is None and ("value" in level or "unit" in level):
                distance = {k: level[k] for k in ("unit", "value", "type") if k in level}
            _check_distance(distance, f"take_profits[{index}].distance", probe, required=True)
            value = distance.get("value") if isinstance(distance, dict) else None
            numeric = as_float(value)
            if numeric is not None:
                if numeric <= previous:
                    probe.error(f"take_profits[{index}] distances must be strictly increasing")
                previous = numeric
            percent = as_float(level.get("close_percent", level.get("percent")))
            if percent is not None:
                if percent <= 0 or percent > 100:
                    probe.error(f"take_profits[{index}].close_percent must be between 0 and 100")
                total += percent
        if total > 100:
            probe.error(f"take_profits close percentages sum to {total:g}, which exceeds 100")

    for message in probe.errors:
        report.warn("ignored by the EA: " + message)


def _parse_expiration(value: Any) -> Optional[bool]:
    """Mirror the EA's ParseDatetimeFlexible: True if it would parse.

    Accepts ISO (``2026-10-31T18:00:00``, trailing ``Z`` ok) or MT5 dot format
    (``2026.10.31 18:00``). Returns None for a non-string, False when the EA
    would reject it (retcode -43), True when it parses. Validating here means an
    unparseable expiry is refused at intake instead of being SKIPPED at the EA.
    """
    if not isinstance(value, str):
        return None
    text = value.strip()
    if len(text) < 16:
        return False
    text = text.replace("T", " ").replace("Z", "")
    if "." not in text and "-" in text and text.index("-") < 6:
        text = text.replace("-", ".")
    parts = text.split(" ")
    if len(parts) < 2:
        return False
    date = parts[0].split(".")
    if len(date) != 3:
        return False
    time = parts[1].split(":")
    if len(time) < 2:
        return False
    try:
        year, month, day = int(date[0]), int(date[1]), int(date[2])
        hour, minute = int(time[0]), int(time[1])
        second = int(time[2]) if len(time) > 2 else 0
    except ValueError:
        return False
    return (2000 <= year <= 2099 and 1 <= month <= 12 and 1 <= day <= 31
            and 0 <= hour <= 23 and 0 <= minute <= 59 and 0 <= second <= 59)


def _check_expiration(value: Any, report: Report) -> None:
    if value is None:
        return
    parsed = _parse_expiration(value)
    if parsed is None:
        report.error("expiration must be a string (ISO '2026-10-31T18:00:00' or MT5 '2026.10.31 18:00')")
    elif parsed is False:
        report.error(
            f"expiration '{value}' is not parseable by the EA; use ISO "
            "'2026-10-31T18:00:00' or MT5 '2026.10.31 18:00' (absent = GTC)"
        )


def _check_positive_int(value: Any, name: str, report: Report) -> None:
    numeric = as_float(value)
    if numeric is None or numeric <= 0 or numeric != int(numeric):
        report.error(f"{name} must be a positive integer")


def _check_pending_entry(command: Dict[str, Any], report: Report, strict: bool, is_legacy: bool, action: str) -> None:
    """Validate a pending-order placement (the six types, or action 'pending')."""
    order_type = command.get("order_type")
    if action == "pending":
        if not order_type:
            report.error(
                "action 'pending' requires an order_type: "
                + ", ".join(sorted(contract.PENDING_ENTRY_ACTIONS))
            )
            return
        resolved = contract.normalize_action(order_type)
        if resolved not in contract.PENDING_ENTRY_ACTIONS:
            report.error(
                f"order_type '{order_type}' is not a pending type; use "
                + ", ".join(sorted(contract.PENDING_ENTRY_ACTIONS))
            )
            return
    else:
        resolved = action
        if order_type and contract.normalize_action(order_type) != resolved:
            report.warn(
                f"order_type '{order_type}' is ignored; the action '{action}' decides the pending type"
            )

    _check_size(command, report, strict, is_legacy)
    _check_distance(command.get("stop_loss"), "stop_loss", report)
    _check_distance(command.get("take_profit"), "take_profit", report)
    if command.get("stop_loss") is not None or command.get("take_profit") is not None:
        report.warn("a pending order's SL/TP are measured from the order price, not the market price")

    price_unit = str(command.get("price_unit") or contract.PRICE_UNIT_ABSOLUTE).strip().lower()
    if price_unit not in contract.PRICE_UNITS:
        report.error(
            f"price_unit '{price_unit}' is unknown; the EA accepts "
            + ", ".join(sorted(contract.PRICE_UNITS))
        )
    price = command.get("price")
    numeric = as_float(price)
    if price is None:
        report.error(
            "a pending order requires a price: an absolute trigger price, or a signed "
            "distance from market when price_unit is 'points'"
        )
    elif numeric is None:
        report.error("price must be numeric")
    elif price_unit in contract.PRICE_UNIT_DISTANCE:
        if numeric == 0:
            report.error(
                "price 0 with price_unit 'points' would sit at the market; use an absolute "
                "price or a non-zero signed offset"
            )
    elif numeric <= 0:
        report.error("an absolute pending price must be greater than zero")

    if resolved in contract.STOP_LIMIT_ACTIONS:
        stoplimit = command.get("stoplimit")
        sl_numeric = as_float(stoplimit)
        if stoplimit is None:
            report.error(f"{resolved} requires a stoplimit (the inner limit price)")
        elif sl_numeric is None or sl_numeric <= 0:
            report.error("stoplimit must be a price greater than zero")

    _check_expiration(command.get("expiration"), report)


def validate_command(command: Dict[str, Any], mode: str = MODE_STRICT) -> Dict[str, Any]:
    """Validate a normalized command. Returns {valid, errors, warnings}."""
    report = Report()

    if "error" in command:
        report.error(str(command["error"]))
        return report.as_dict()

    strict = mode != MODE_PERMISSIVE
    if mode not in VALID_MODES:
        report.warn(f"unknown validation mode '{mode}'; using strict")
        strict = True

    action = contract.normalize_action(command.get("action"))

    if not action:
        report.error("action is required")
        return report.as_dict()

    if action in contract.SKIPPED_ACTIONS:
        report.error(
            f"action '{action}' is not implemented by EA v{contract.EA_VERSION} "
            f"({contract.SKIPPED_ACTIONS[action]}); it would be acknowledged as SKIPPED (-13)"
        )
        return report.as_dict()

    if action not in contract.EXECUTED_ACTIONS:
        report.error(
            f"unsupported action '{action}'; EA v{contract.EA_VERSION} executes "
            + ", ".join(sorted(contract.EXECUTED_ACTIONS))
        )
        return report.as_dict()

    # Legacy commands deliberately omit the symbol: the EA substitutes its
    # chart symbol, which is the documented v1 behaviour.
    is_legacy = bool((command.get("_meta") or {}).get("is_legacy")) or command.get("schema_version") == "1.0"

    symbol = command.get("symbol")
    if action in _SYMBOL_REQUIRED_FOR:
        if not symbol:
            if action in ("close_position", "partial_close", "modify", "breakeven", "trailing") and command.get("position_ticket"):
                # Position actions can target a specific position_ticket directly
                pass
            elif strict and not is_legacy:
                report.error(
                    "symbol is required in strict mode - without it the EA trades "
                    "its own chart symbol, which may not be the intended instrument"
                )
            else:
                report.warn("symbol omitted; the EA will use its chart symbol")
        elif not isinstance(symbol, str):
            report.error("symbol must be a string")

    if action in contract.ENTRY_ACTIONS:
        _check_size(command, report, strict, is_legacy)
        _check_distance(command.get("stop_loss"), "stop_loss", report)
        _check_distance(command.get("take_profit"), "take_profit", report)

    if action in contract.PENDING_ACTIONS:
        _check_pending_entry(command, report, strict, is_legacy, action)

    if action == "cancel_pending":
        # The EA cancels by order_ticket, or by symbol + magic when no ticket is
        # given. Symbol "*" (or omitted with a ticket) is the account-wide case.
        if not command.get("order_ticket") and not symbol:
            report.error("cancel_pending requires an order_ticket or a symbol")
        if command.get("order_ticket") is not None:
            _check_positive_int(command.get("order_ticket"), "order_ticket", report)

    if action == "modify_pending":
        if not command.get("order_ticket") and not symbol:
            report.error("modify_pending requires an order_ticket or a symbol")
        if command.get("order_ticket") is not None:
            _check_positive_int(command.get("order_ticket"), "order_ticket", report)
        # A modify that changes nothing is a mistake, not a no-op to forward.
        touches_price = command.get("price") is not None
        touches_sl = command.get("stop_loss") is not None
        touches_tp = command.get("take_profit") is not None
        touches_expiry = command.get("expiration") is not None
        if not (touches_price or touches_sl or touches_tp or touches_expiry):
            report.error(
                "modify_pending requires at least one of price, stop_loss, "
                "take_profit or expiration"
            )
        if touches_price:
            price_numeric = as_float(command.get("price"))
            price_unit = str(command.get("price_unit") or contract.PRICE_UNIT_ABSOLUTE).strip().lower()
            if price_numeric is None:
                report.error("price must be numeric")
            elif price_unit in contract.PRICE_UNIT_DISTANCE:
                if price_numeric == 0:
                    report.error("price 0 with price_unit 'points' would not move the order")
            elif price_numeric <= 0:
                report.error("an absolute pending price must be greater than zero")
        _check_distance(command.get("stop_loss"), "stop_loss", report)
        _check_distance(command.get("take_profit"), "take_profit", report)
        _check_expiration(command.get("expiration"), report)

    if action == "partial_close":
        close_volume = as_float(command.get("close_volume"))
        percent = as_float(command.get("close_percent"))
        if close_volume is not None:
            if close_volume <= 0:
                report.error("close_volume must be greater than 0")
        elif percent is None:
            report.warn("close_percent omitted; the EA closes 100% of the position")
        elif not 0 < percent <= 100:
            report.error("close_percent must be greater than 0 and no more than 100")

    if action == "modify":
        has_sl = command.get("stop_loss") is not None
        has_tp = command.get("take_profit") is not None
        if not has_sl and not has_tp:
            report.error("modify requires at least one of stop_loss or take_profit")
        _check_distance(command.get("stop_loss"), "stop_loss", report)
        _check_distance(command.get("take_profit"), "take_profit", report)

    if action == "close_position":
        if not symbol and not command.get("position_ticket"):
            report.error("close_position requires a symbol or position_ticket")

    if action == "breakeven":
        breakeven = command.get("breakeven")
        if isinstance(breakeven, dict):
            lock_in = breakeven.get("lock_in")
            if lock_in is not None:
                _check_distance(lock_in, "breakeven.lock_in", report)
            trigger = breakeven.get("trigger")
            if trigger is not None:
                _check_distance(trigger, "breakeven.trigger", report)
        else:
            report.warn("breakeven without a lock_in uses the EA's 10-point default")

    if action == "trailing":
        trailing = command.get("trailing")
        if isinstance(trailing, dict):
            if trailing.get("distance") is not None:
                _check_distance(trailing.get("distance"), "trailing.distance", report)
            if trailing.get("activation") is not None:
                _check_distance(trailing.get("activation"), "trailing.activation", report)

    if action != "trailing" and command.get("trailing") is not None:
        report.warn("trailing on entry is recorded; send action 'trailing' to advance trailing stop")

    if command.get("take_profits") is not None:
        _check_take_profits(command["take_profits"], report)

    # Accepted for any action, validated when present.
    risk_percent = as_float(command.get("risk_percent"))
    if risk_percent is not None and not 0 < risk_percent <= 100:
        report.error("risk_percent must be greater than 0 and no more than 100")

    magic = command.get("magic")
    if magic is not None:
        magic_number = as_float(magic)
        if magic_number is None or magic_number != int(magic_number) or int(magic_number) < 0:
            report.error("magic must be a non-negative integer")
        else:
            report.warn(
                "magic is forwarded, but EA v6.5 uses its own MagicNumber input for "
                "position ownership; a mismatched magic will not be honoured"
            )

    price = command.get("price")
    if price is not None and action in contract.ENTRY_ACTIONS:
        # A market buy/sell executes at the current price, so a stated price is
        # carried for the record but never used as a limit. Pending orders are
        # validated separately (_check_pending_entry) and legitimately use price,
        # including signed distances, so they are excluded here.
        numeric = as_float(price)
        if numeric is None:
            report.error("price must be numeric")
        elif not (command.get("_meta") or {}).get("is_journal"):
            report.warn("price is ignored for a market entry; EA v6.5 executes buy/sell at market")

    for extra in (command.get("_meta") or {}).get("warnings", []):
        report.warn(extra)

    return report.as_dict()
