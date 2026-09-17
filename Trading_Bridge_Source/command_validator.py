from __future__ import annotations

from typing import Any, Dict, List

ENTRY_ACTIONS = {"buy", "sell", "buy_limit", "sell_limit", "buy_stop", "sell_stop"}
MANAGEMENT_ACTIONS = {"close_position", "close_positions", "partial_close", "modify_position", "reverse", "close_pending"}
VALID_ACTIONS = ENTRY_ACTIONS | MANAGEMENT_ACTIONS


def validate_command(command: Dict[str, Any]) -> Dict[str, Any]:
    errors: List[str] = []
    action = str(command.get("action", "")).lower()
    is_legacy = command.get("schema_version") == "1.0"

    if action not in VALID_ACTIONS:
        errors.append(f"unsupported action: {action or '<missing>'}")

    if not is_legacy and not command.get("symbol") and action not in {"close_position"}:
        errors.append("symbol is required for structured commands")

    if action in ENTRY_ACTIONS:
        size = command.get("size", {})
        value = size.get("value") if isinstance(size, dict) else size
        try:
            if float(value) <= 0:
                errors.append("size must be greater than zero")
        except (TypeError, ValueError):
            errors.append("size must be numeric")

    if action in {"buy_limit", "sell_limit", "buy_stop", "sell_stop"}:
        if command.get("price") is None:
            errors.append("price is required for pending orders")

    take_profits = command.get("take_profits", [])
    if take_profits:
        if not isinstance(take_profits, list):
            errors.append("take_profits must be an array")
        else:
            total = 0.0
            previous = 0.0
            for level in take_profits:
                if not isinstance(level, dict):
                    errors.append("each take-profit level must be an object")
                    continue
                total += float(level.get("close_percent", 0))
                distance = level.get("distance", {})
                value = distance.get("value", 0) if isinstance(distance, dict) else 0
                if value <= previous:
                    errors.append("take-profit distances must be strictly increasing")
                previous = value
            if total <= 0 or total > 100:
                errors.append("take-profit close percentages must total between 0 and 100")

    risk = command.get("risk", {})
    if isinstance(risk, dict) and risk.get("risk_percent") is not None:
        try:
            if not 0 < float(risk["risk_percent"]) <= 100:
                errors.append("risk_percent must be greater than 0 and no more than 100")
        except (TypeError, ValueError):
            errors.append("risk_percent must be numeric")

    return {"valid": not errors, "errors": errors}
