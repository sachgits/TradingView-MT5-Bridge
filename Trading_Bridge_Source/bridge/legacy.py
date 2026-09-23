"""Parser for the legacy plain-text signal format.

The Chrome extension and older producers emit a flat string such as
``BUY SL=50 TP=100 LOT=0.01``. Bot_Trader has its own copy of this logic in
ParseLegacyCommand(); this mirror exists so the bridge can validate and
display a legacy signal as a structured command before it is dispatched.

The two must agree. Differences are covered by tests/test_legacy.py.
"""

from __future__ import annotations

import re
from typing import Any, Dict, Optional

# ParseLegacyCommand() looks for SELL/SHORT and treats everything else as a
# buy, so LONG is accepted but is not itself a discriminator.
_SIDE_RE = re.compile(r"\b(BUY|SELL|LONG|SHORT)\b", re.IGNORECASE)
_TOKEN_RE = r"\b{name}\s*=\s*([-+]?\d+(?:\.\d+)?)"


def _token(text: str, name: str) -> Optional[float]:
    found = re.search(_TOKEN_RE.format(name=name), text, re.IGNORECASE)
    if not found:
        return None
    try:
        return float(found.group(1))
    except ValueError:
        return None


def looks_legacy(value: Any) -> bool:
    """True when a ``signal`` value is plain text rather than embedded JSON.

    Mirrors the EA's own discriminator at ParseTradeCommand(): a signal
    containing ``{`` is a JSON-encoded command, not legacy text. Without this
    the bridge would feed JSON to a regex that expects ``SL=``/``TP=`` tokens.
    """
    if not isinstance(value, str):
        return False
    text = value.strip()
    return bool(text) and "{" not in text


def parse_legacy_signal(text: str) -> Dict[str, Any]:
    """Convert plain text to a structured command.

    Only the tokens the sender actually wrote become fields. Omitting ``LOT=``
    must leave ``size`` absent so the EA applies its own ``LotSize`` input -
    filling in a bridge-side default here would silently override what the
    operator configured in MT5. The same reasoning applies to SL/TP.

    ``symbol`` stays absent for the same reason: a legacy signal carries no
    symbol, and the EA substitutes its chart symbol. Inventing one would route
    the trade to the wrong instrument.
    """
    raw = str(text or "").strip()
    match = _SIDE_RE.search(raw)
    if not match:
        return {"error": "legacy signal must contain BUY, SELL, LONG or SHORT"}

    side = match.group(1).upper()
    action = "sell" if side in {"SELL", "SHORT"} else "buy"

    sl = _token(raw, "SL")
    tp = _token(raw, "TP")
    lot = _token(raw, "LOT")

    command: Dict[str, Any] = {
        "schema_version": "1.0",
        "action": action,
        "legacy_signal": raw,
    }
    if lot is not None:
        command["size"] = {"type": "fixed", "unit": "lots", "value": lot}
    if sl is not None:
        command["stop_loss"] = {"type": "distance", "unit": "points", "value": sl}
    if tp is not None:
        command["take_profit"] = {"type": "distance", "unit": "points", "value": tp}

    # Recorded so the signal board can show which values the EA's inputs will
    # supply rather than leaving the operator to guess.
    command["defaults_from_ea"] = sorted(
        name for name, value in (("SL", sl), ("TP", tp), ("LOT", lot)) if value is None
    )
    return command
