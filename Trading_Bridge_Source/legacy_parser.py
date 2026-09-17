from __future__ import annotations

import re
from typing import Any, Dict


def parse_legacy_signal(text: str) -> Dict[str, Any]:
    text = str(text or "").strip()
    match = re.search(r"\b(BUY|SELL|LONG|SHORT)\b", text, re.IGNORECASE)
    if not match:
        return {"error": "legacy signal must contain BUY, SELL, LONG, or SHORT"}

    action = "sell" if match.group(1).upper() in {"SELL", "SHORT"} else "buy"

    def read(name: str, default: float) -> float:
        found = re.search(rf"\b{name}\s*=\s*([-+]?\d+(?:\.\d+)?)", text, re.IGNORECASE)
        return float(found.group(1)) if found else default

    return {
        "schema_version": "1.0",
        "action": action,
        "symbol": None,
        "size": {"type": "fixed", "unit": "lots", "value": read("LOT", 0.01)},
        "stop_loss": {"type": "distance", "unit": "points", "value": read("SL", 50)},
        "take_profit": {"type": "distance", "unit": "points", "value": read("TP", 100)},
        "legacy_signal": text,
    }
