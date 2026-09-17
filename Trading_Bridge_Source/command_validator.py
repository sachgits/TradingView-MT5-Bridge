from __future__ import annotations

from typing import Any, Dict, List

ENTRY_ACTIONS = {"buy", "sell", "buy_limit", "sell_limit", "buy_stop", "sell_stop"}
MANAGEMENT_ACTIONS = {"close_position", "close_positions", "partial_close", "modify_position", "reverse", "close_pending"}
VALID_ACTIONS = ENTRY_ACTIONS | MANAGEMENT_ACTIONS
DISTANCE_UNITS = {"points", "price_distance", "absolute_price", "percent", "currency", "atr"}


def _float(value: Any, field: str, errors: List[str]) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        errors.append(f"{field} must be numeric")
        return None


def _validate_distance(spec: Any, name: str, errors: List[str]) -> None:
    if not isinstance(spec, dict):
        errors.append(f"{name} must be an object")
        return
    unit = str(spec.get("unit", spec.get("type", "points"))).lower()
    if unit not in DISTANCE_UNITS:
        errors.append(f"{name} has unsupported unit: {unit}")
    if unit == "atr":
        period = _float(spec.get("period", 14), f"{name}.period", errors)
        multiplier = _float(spec.get("multiplier", 1), f"{name}.multiplier", errors)
        if period is not None and period < 1:
            errors.append(f"{name}.period must be at least 1")
        if multiplier is not None and multiplier <= 0:
            errors.append(f"{name}.multiplier must be greater than zero")
    elif spec.get("value") is None:
        errors.append(f"{name}.value is required")
    else:
        value = _float(spec.get("value"), f"{name}.value", errors)
        if value is not None and value <= 0:
            errors.append(f"{name}.value must be greater than zero")


def validate_command(command: Dict[str, Any]) -> Dict[str, Any]:
    errors: List[str] = []
    action = str(command.get("action", "")).lower().strip()
    legacy = command.get("schema_version") == "1.0"

    if action not in VALID_ACTIONS:
        errors.append(f"unsupported action: {action or '<missing>'}")

    # A permissive parser is useful; permissive live execution is unsafe.
    if not legacy and action in ENTRY_ACTIONS and not command.get("symbol"):
        errors.append("symbol is required for structured entry commands")

    if action in ENTRY_ACTIONS:
        size = command.get("size")
        value = size.get("value") if isinstance(size, dict) else size
        parsed = _float(value, "size", errors)
        if parsed is not None and parsed <= 0:
            errors.append("size must be greater than zero")

        if command.get("stop_loss") is not None:
            _validate_distance(command["stop_loss"], "stop_loss", errors)
        if command.get("take_profit") is not None:
            _validate_distance(command["take_profit"], "take_profit", errors)

    if action in {"buy_limit", "sell_limit", "buy_stop", "sell_stop"}:
        if command.get("price") is None:
            errors.append("price is required for pending orders")
        else:
            price = _float(command["price"], "price", errors)
            if price is not None and price <= 0:
                errors.append("price must be greater than zero")

    if action == "close_position" and not command.get("position_ticket"):
        errors.append("position_ticket is required for close_position")
    if action in {"close_positions", "partial_close", "modify_position"}:
        if not command.get("symbol") and not command.get("scope"):
            errors.append("symbol or scope is required for position management")

    levels = command.get("take_profits")
    if levels is not None:
        if not isinstance(levels, list) or not levels:
            errors.append("take_profits must be a non-empty array")
        else:
            total = 0.0
            previous = 0.0
            for index, level in enumerate(levels, 1):
                if not isinstance(level, dict):
                    errors.append(f"take_profits[{index}] must be an object")
                    continue
                distance = level.get("distance")
                _validate_distance(distance, f"take_profits[{index}].distance", errors)
                value = distance.get("value") if isinstance(distance, dict) else None
                if isinstance(value, (int, float)) and value <= previous:
                    errors.append("take-profit numeric distances must be strictly increasing")
                if isinstance(value, (int, float)):
                    previous = float(value)
                percent = _float(level.get("close_percent"), f"take_profits[{index}].close_percent", errors)
                if percent is not None:
                    if percent <= 0 or percent > 100:
                        errors.append("take-profit close_percent must be between 0 and 100")
                    total += percent
            if total > 100:
                errors.append("take-profit close percentages cannot exceed 100")

    risk = command.get("risk")
    if isinstance(risk, dict) and risk.get("risk_percent") is not None:
        value = _float(risk["risk_percent"], "risk.risk_percent", errors)
        if value is not None and not 0 < value <= 100:
            errors.append("risk.risk_percent must be greater than 0 and no more than 100")

    return {"valid": not errors, "errors": errors}
