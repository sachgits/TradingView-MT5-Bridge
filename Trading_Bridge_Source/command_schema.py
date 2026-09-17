from __future__ import annotations

import json
import re
from copy import deepcopy
from typing import Any, Dict

from legacy_parser import parse_legacy_signal


def _number(value: Any, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _distance(value: Any, default: float) -> Dict[str, Any]:
    if isinstance(value, dict):
        result = deepcopy(value)
        result.setdefault("type", "distance")
        result.setdefault("unit", "points")
        if "value" not in result and "distance" in result:
            result["value"] = result["distance"]
        if result.get("type") == "atr":
            result.setdefault("period", 14)
            result.setdefault("timeframe", "M15")
            result.setdefault("multiplier", 1.0)
        return result
    return {"type": "distance", "unit": "points", "value": _number(value, default)}


def _size(value: Any) -> Dict[str, Any]:
    if isinstance(value, dict):
        result = deepcopy(value)
        result.setdefault("type", "fixed")
        result.setdefault("unit", "lots")
        return result
    return {"type": "fixed", "unit": "lots", "value": _number(value, 0.01)}


def normalize_command(payload: Any) -> Dict[str, Any]:
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except json.JSONDecodeError:
            return parse_legacy_signal(payload)
    if not isinstance(payload, dict):
        return {"error": "payload must be a JSON object or legacy signal string"}

    if payload.get("signal"):
        legacy = parse_legacy_signal(str(payload["signal"]))
        if "error" not in legacy:
            legacy.update({k: v for k, v in payload.items() if k != "signal"})
            return legacy
        return legacy

    action = str(payload.get("action", "")).strip().lower()
    if not action:
        return {"error": "missing action"}

    result: Dict[str, Any] = {
        "schema_version": str(payload.get("schema_version", "2.0")),
        "request_id": payload.get("request_id"),
        "timestamp": payload.get("timestamp"),
        "action": action,
        "symbol": payload.get("symbol"),
        "strategy_id": payload.get("strategy_id"),
        "magic": payload.get("magic"),
        "execution_mode": payload.get("execution_mode"),
    }

    if "size" in payload:
        result["size"] = _size(payload["size"])
    elif "lot_size" in payload or "lots" in payload:
        result["size"] = _size(payload.get("lot_size", payload.get("lots")))
    elif action in {"buy", "sell", "buy_limit", "sell_limit", "buy_stop", "sell_stop"}:
        result["size"] = _size(0.01)

    if "sl" in payload:
        result["stop_loss"] = _distance(payload["sl"], 50)
    elif "stop_loss" in payload:
        result["stop_loss"] = _distance(payload["stop_loss"], 50)
    elif "sl_distance" in payload:
        result["stop_loss"] = _distance(payload["sl_distance"], 50)

    if "tp" in payload:
        result["take_profit"] = _distance(payload["tp"], 100)
    elif "take_profit" in payload:
        result["take_profit"] = _distance(payload["take_profit"], 100)
    elif "tp_distance" in payload:
        result["take_profit"] = _distance(payload["tp_distance"], 100)

    if "take_profits" in payload:
        result["take_profits"] = payload["take_profits"]
    for key in ("breakeven", "trailing", "risk", "scope", "position_ticket", "close_percent", "price", "expiration", "reason"):
        if key in payload:
            result[key] = payload[key]

    return result
