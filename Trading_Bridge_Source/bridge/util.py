"""Small helpers shared across the bridge."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any, Optional


def utc_now() -> str:
    """UTC timestamp in the ISO-8601 form the EA's staleness check parses."""
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def to_ea_timestamp(stored: Optional[str]) -> Optional[str]:
    """Convert a stored SQLite timestamp to the form Bot_Trader ages signals with.

    The store keeps ``YYYY-MM-DD HH:MM:SS`` so SQLite's own date arithmetic
    works. ParseSignalTimeStamp() in the EA expects ``YYYY-MM-DDTHH:MM:SSZ``
    and rewrites ``-`` to ``.`` and ``T`` to a space; anything else makes
    IsSignalStale() return false and disables stale-signal cleanup.
    """
    if not stored:
        return None
    text = str(stored).strip()
    if not text:
        return None
    if "T" not in text:
        text = text.replace(" ", "T", 1)
    if not text.endswith("Z"):
        text += "Z"
    return text


def as_float(value: Any) -> Optional[float]:
    """Coerce to float, returning None when it is not a number.

    ``bool`` is rejected explicitly: ``isinstance(True, int)`` in Python, so
    ``{"size": true}`` would otherwise normalize to 1.0 lots.
    """
    if isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def canonical_json(value: Any) -> str:
    """Deterministic JSON, so the same command always hashes the same."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def content_hash(command: Any) -> str:
    """Stable digest of a command, ignoring fields that differ per submission.

    ``request_id`` and ``timestamp`` identify a submission, not a trade
    intent, so they are excluded: dropping the same file twice must dedup even
    if the producer stamped a fresh timestamp each time.
    """
    payload = dict(command) if isinstance(command, dict) else {"value": command}
    for key in ("request_id", "timestamp", "_meta", "source_file", "received_at"):
        payload.pop(key, None)
    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()
