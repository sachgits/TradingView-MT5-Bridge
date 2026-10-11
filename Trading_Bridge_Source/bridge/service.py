"""Intake and dispatch coordination.

This is the only module that decides whether a command reaches Bot_Trader.
Everything else - the folder watcher, the HTTP endpoints, the dashboard - is a
way of getting a payload in or a status out.

The pipeline for one submission is:

    normalize -> validate -> dedup -> queue-depth -> ledger -> event

Two rules govern it, and both come from the fact that this bridge places real
orders on a broker account:

* **Never invent a trade parameter.** A command that does not state its own
  symbol or size is rejected in strict mode rather than filled in from a
  default. Guessing is how a bridge opens a position nobody asked for.
* **Never send the same command twice.** Every submission is recorded in SQLite
  before it is dispatched and updated with the EA's acknowledgement after, so
  "already handled" survives a restart. When the outcome is genuinely unknown -
  a dispatch the EA never acknowledged - the signal is marked expired and left
  for an operator to retry, because a duplicate execution is worse than a
  missed trade.
"""

from __future__ import annotations

import json
import re
import threading
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from . import contract, events, schema, validator
from .config import MUTABLE, Settings, mutable_view
from .events import EventBus
from .store import RETRYABLE_STATUSES, Store
from .util import content_hash, parse_iso, to_ea_timestamp, utc_now

# Bot_Trader polls every PollIntervalMs (750 ms by default) and acknowledges in
# the same request, so anything much longer than a few polls means it stopped.
EA_ONLINE_WINDOW_SECONDS = 30

_REQUEST_ID_RE = re.compile(r"[^A-Za-z0-9._-]")
_MAX_REQUEST_ID_LEN = 128

# Acknowledgement status -> bridge-side lifecycle status.
_ACK_STATUS = {
    contract.RESULT_SUCCESS: contract.STATUS_EXECUTED,
    contract.RESULT_FAILED: contract.STATUS_FAILED,
    contract.RESULT_SKIPPED: contract.STATUS_SKIPPED,
}

# Returned by submit() when an identical command was already handled. Not a
# ledger status: a duplicate gets no row of its own, it points at the row that
# already exists.
DUPLICATE = "duplicate"


def public_command(command: Any) -> Any:
    """A command without the bridge's own bookkeeping keys.

    ``_meta`` records how the bridge understood a payload - whether it came in
    as legacy text, which fields were unknown. It is kept in the ledger and
    shown on the board, but it must never go to the EA: Bot_Trader's JSON reader
    searches the whole body for the first occurrence of a key, so any extra
    nesting is a chance to mis-read the command.
    """
    if not isinstance(command, dict):
        return command
    return {k: v for k, v in command.items() if not k.startswith("_")}


def _parse_iso(text: Optional[str]) -> Optional[datetime]:
    if not text:
        return None
    try:
        return datetime.fromisoformat(str(text).replace("Z", "+00:00"))
    except ValueError:
        return None


class Service:
    """Owns the settings, the ledger and the event bus for one bridge process."""

    def __init__(self, settings: Settings, store: Store, bus: Optional[EventBus] = None) -> None:
        self.settings = settings
        self.store = store
        self.bus = bus or EventBus()
        self._lock = threading.RLock()
        self._watcher: Any = None
        self._ea_online: Optional[bool] = None
        # Account-snapshot polling state. _account_inflight_at is the monotonic
        # time an "account" request was handed to the EA (None when nothing is
        # outstanding); it stops the bridge re-asking on every 750 ms poll while
        # a snapshot is still coming back. _account_force is set by an on-demand
        # refresh so the next poll injects one regardless of age.
        self._account_inflight_at: Optional[float] = None
        self._account_request_id: Optional[str] = None
        self._account_force = False
        self._restore_settings()

    # ---------------------------------------------------------------- wiring
    def attach_watcher(self, watcher: Any) -> None:
        self._watcher = watcher

    @property
    def watcher(self) -> Any:
        """The folder watcher, or None when file intake runs in another process."""
        return self._watcher

    def _restore_settings(self) -> None:
        """Re-apply runtime settings persisted before the last restart.

        Anything the operator pinned on the command line is left alone: an
        explicit ``--folder`` means what it says, and quietly replacing it with
        last week's browser edit would point intake somewhere unexpected.
        """
        pinned = set(getattr(self.settings, "pinned", ()))
        stored = self.store.load_settings(set(MUTABLE) - pinned)
        if not stored:
            return
        # clamp() runs inside apply(), so an out-of-range value saved by an
        # older build cannot survive a restart.
        self.settings.apply(stored)

    def emit(self, event_type: str, request_id: Optional[str] = None, detail: Any = None) -> Dict[str, Any]:
        """Record an event in the ledger, then fan it out to dashboards.

        The write happens first and the publish second, so a dropped or slow
        subscriber can only ever lose a notification, never the record. Clients
        resynchronise by re-reading the table from their last seen id.
        """
        row_id = self.store.add_event(event_type, request_id, detail)
        event = {
            "id": row_id,
            "type": event_type,
            "request_id": request_id,
            "detail": detail if isinstance(detail, (str, type(None))) else json.dumps(detail, default=str),
            "created_at": utc_now(),
        }
        self.bus.publish(event)
        return event

    # ----------------------------------------------------------------- intake
    def submit(
        self,
        payload: Any,
        source: str = contract.SOURCE_API,
        *,
        source_file: Optional[str] = None,
        request_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Validate and queue one command. Returns a JSON-ready result dict.

        ``ok`` is True only when the command is queued for the EA. A rejected or
        malformed command still gets a ledger row and an event: the board has to
        show what was refused, not just what traded.
        """
        raw = payload
        if isinstance(raw, (bytes, bytearray)):
            try:
                raw = raw.decode("utf-8")
            except UnicodeDecodeError:
                raw = None

        command = schema.normalize_command(raw if raw is not None else payload)
        if "error" in command:
            return self._record_failure(
                contract.STATUS_INVALID,
                command["error"],
                raw_payload=payload,
                command=command,
                source=source,
                source_file=source_file,
                request_id=self._new_request_id(request_id or self._payload_request_id(payload)),
            )

        rid = self._new_request_id(request_id or command.get("request_id"))
        report = validator.validate_command(command, self.settings.validation_mode)
        stale = self._journal_stale_error(command)
        if stale is not None:
            report = dict(report, valid=False, errors=list(report["errors"]) + [stale])
        # The validator folds normalization's structural notes (_meta.warnings)
        # into its own report, so this is the complete list.
        warnings = list(report["warnings"])
        warnings += schema.shadowing_risk(command)

        if not report["valid"]:
            return self._record_failure(
                contract.STATUS_REJECTED,
                "; ".join(report["errors"]),
                raw_payload=payload,
                command=command,
                source=source,
                source_file=source_file,
                request_id=rid,
                warnings=warnings,
            )

        digest = content_hash(command)
        existing = self.store.find_duplicate(digest, rid, self.settings.dedup_window_seconds)
        if existing is not None:
            if existing["request_id"] == rid and existing["status"] in RETRYABLE_STATUSES:
                # The producer reused an idempotency key for a command that never
                # reached the broker. Revive that row instead of refusing it.
                self.store.revive_signal(
                    rid, command=command, raw_payload=payload, dedup_hash=digest, warnings=warnings
                )
                summary = schema.summarize(command)
                self.emit(events.SIGNAL_REQUEUED, rid, {"summary": summary, "reason": "resubmitted request_id"})
                return {
                    "ok": True,
                    "status": contract.STATUS_QUEUED,
                    "request_id": rid,
                    "command": public_command(command),
                    "warnings": warnings,
                    "errors": [],
                    "summary": summary,
                    "revived": True,
                }
            self.emit(
                events.SIGNAL_DUPLICATE,
                rid,
                {
                    "existing_request_id": existing["request_id"],
                    "existing_status": existing["status"],
                    "summary": schema.summarize(command),
                },
            )
            return {
                "ok": False,
                "status": DUPLICATE,
                "request_id": rid,
                "duplicate_of": existing["request_id"],
                "duplicate_status": existing["status"],
                "command": public_command(command),
                "warnings": warnings,
                "errors": [f"duplicate of {existing['request_id']} ({existing['status']})"],
                "summary": schema.summarize(command),
            }

        depth = self.store.queue_depth()
        if depth >= self.settings.max_queue_depth:
            return self._record_failure(
                contract.STATUS_REJECTED,
                f"queue full ({depth} waiting, max {self.settings.max_queue_depth})",
                raw_payload=payload,
                command=command,
                source=source,
                source_file=source_file,
                request_id=rid,
                warnings=warnings,
            )

        signal_id = self.store.insert_signal(
            request_id=rid,
            dedup_hash=digest,
            source=source,
            command=command,
            raw_payload=payload,
            status=contract.STATUS_QUEUED,
            warnings=warnings,
            source_file=source_file,
        )
        summary = schema.summarize(command)
        self.emit(
            events.SIGNAL_RECEIVED,
            rid,
            {"summary": summary, "source": source, "source_file": source_file, "id": signal_id},
        )
        return {
            "ok": True,
            "status": contract.STATUS_QUEUED,
            "request_id": rid,
            "id": signal_id,
            "command": public_command(command),
            "warnings": warnings,
            "errors": [],
            "summary": summary,
            "queue_depth": depth + 1,
        }

    def reject_intake(
        self,
        error: str,
        *,
        source: str,
        source_file: Optional[str] = None,
        raw_payload: Any = None,
    ) -> Dict[str, Any]:
        """Ledger a payload that could not even be read as a command.

        Kept separate from submit() because submit() hands unparseable text to
        the legacy parser, which finds a BUY or SELL keyword almost anywhere. A
        truncated JSON file contains ``"action": "buy"``, so treating it as
        legacy text would queue a real market order from a corrupt file. Callers
        that know the payload was meant to be JSON must come here instead.
        """
        command = {"error": error}
        if isinstance(raw_payload, str):
            command["text"] = raw_payload[:2000]
        return self._record_failure(
            contract.STATUS_INVALID,
            error,
            raw_payload=raw_payload,
            command=command,
            source=source,
            source_file=source_file,
            request_id=self._new_request_id(None),
        )

    def _record_failure(
        self,
        status: str,
        error: str,
        *,
        raw_payload: Any,
        command: Dict[str, Any],
        source: str,
        source_file: Optional[str],
        request_id: str,
        warnings: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        """Ledger a command that will not be dispatched."""
        signal_id = self.store.insert_signal(
            request_id=request_id,
            dedup_hash=content_hash(command),
            source=source,
            command=command,
            raw_payload=raw_payload,
            status=status,
            warnings=warnings or [],
            error=error,
            source_file=source_file,
        )
        event_type = events.SIGNAL_INVALID if status == contract.STATUS_INVALID else events.SIGNAL_REJECTED
        self.emit(
            event_type,
            request_id,
            {"error": error, "source": source, "source_file": source_file, "id": signal_id},
        )
        return {
            "ok": False,
            "status": status,
            "request_id": request_id,
            "id": signal_id,
            "command": public_command(command),
            "warnings": warnings or [],
            "errors": [error],
            "summary": schema.summarize(command) if isinstance(command, dict) else "",
        }

    @staticmethod
    def _payload_request_id(payload: Any) -> Optional[str]:
        if isinstance(payload, dict):
            value = payload.get("request_id")
            return value if isinstance(value, str) else None
        return None

    @staticmethod
    def _new_request_id(supplied: Optional[str]) -> str:
        if supplied:
            cleaned = _REQUEST_ID_RE.sub("-", str(supplied).strip())[:_MAX_REQUEST_ID_LEN]
            if cleaned:
                return cleaned
        return f"br-{datetime.now(timezone.utc):%Y%m%d%H%M%S}-{uuid.uuid4().hex[:8]}"

    # ------------------------------------------------------------- EA side
    def ea_poll(self) -> Dict[str, Any]:
        """Answer Bot_Trader's ``GET ServerURL``.

        Returns the envelope documented in the EA header: ``status`` NEW with a
        command, or PROCESSED with ``signal`` NONE when there is nothing to do.
        """
        self._note_ea_seen()
        self._expire_overdue()

        if self.settings.paused:
            # Pause means nothing at all is handed to the EA - not even a
            # read-only snapshot. The board keeps showing the last stored one.
            return self._idle_envelope("paused")

        # A real order always takes the poll. Only when nothing is waiting does
        # the bridge spend the poll on a read-only account snapshot, so the
        # positions view can never delay a trade.
        row = self.store.claim_next()
        if row is not None:
            command = row["command"] if isinstance(row["command"], dict) else {}
            self.emit(
                events.SIGNAL_DISPATCHED,
                row["request_id"],
                {"summary": schema.summarize(command), "queue_ms": row.get("queue_ms")},
            )
            return self._build_envelope(row, command)

        account = self._account_envelope_if_due()
        if account is not None:
            return account

        return self._idle_envelope()

    def _idle_envelope(self, reason: str = "") -> Dict[str, Any]:
        envelope = {
            "status": contract.ENVELOPE_IDLE,
            "signal": "NONE",
            "timestamp": utc_now(),
            "request_id": "",
            "source": "bridge",
        }
        if reason:
            envelope["reason"] = reason
        return envelope

    def _build_envelope(self, row: Dict[str, Any], command: Dict[str, Any]) -> Dict[str, Any]:
        """Assemble the GET /signal response for one claimed command.

        Key order matters. Bot_Trader's JSON reader finds the *first* textual
        occurrence of a key anywhere in the body, so the envelope's own
        ``request_id`` and ``timestamp`` must precede the nested ``normalized``
        object or the EA could echo back a value from inside the command.

        ``signal`` carries the same command as an escaped JSON string for
        older builds of the EA. It is placed last and it is safe there: inside
        an escaped string a key reads ``\\"action\\"``, so a search for
        ``"action"`` cannot match it.
        """
        for_ea = public_command(command)
        return {
            "status": contract.ENVELOPE_NEW,
            "request_id": row["request_id"],
            "timestamp": to_ea_timestamp(row.get("created_at")) or utc_now(),
            "source": row.get("source") or contract.SOURCE_API,
            "source_file": row.get("source_file") or "",
            "normalized": for_ea,
            "signal": json.dumps(for_ea, separators=(",", ":"), default=str),
        }

    # -------------------------------------------------- account snapshot poll
    def _account_snapshot_stale(self, interval: int) -> bool:
        """True when the stored snapshot is older than ``interval`` seconds."""
        _, at = self.store.load_account_snapshot()
        if not at:
            return True
        when = parse_iso(at)
        if when is None:
            return True
        age = (datetime.now(timezone.utc) - when).total_seconds()
        return age >= interval

    def _account_envelope_if_due(self) -> Optional[Dict[str, Any]]:
        """Build an ``account`` request envelope, or None if one is not due.

        The EA dedups on the *entire* response body, so each request carries a
        fresh request_id and timestamp - otherwise a second ``{"action":"account"}``
        would be byte-identical to the last and silently skipped. An outstanding
        request blocks a new one until it is acked or times out, which is what
        keeps a 750 ms poll loop from asking for a snapshot every tick.
        """
        interval = max(0, int(self.settings.account_poll_seconds))
        timeout = max(self.settings.dispatch_timeout_seconds, 5)
        with self._lock:
            now = time.monotonic()
            if self._account_inflight_at is not None:
                if now - self._account_inflight_at >= timeout:
                    # The EA never answered the last ask (it may have gone
                    # offline). Stop waiting so a fresh one can be sent.
                    self._account_inflight_at = None
                elif not self._account_force:
                    return None
            if not self._account_force:
                if interval == 0 or not self._account_snapshot_stale(interval):
                    return None
            self._account_force = False
            self._account_inflight_at = now
            request_id = self._new_request_id(None)
            self._account_request_id = request_id

        command = {"action": "account"}
        for_ea = public_command(command)
        return {
            "status": contract.ENVELOPE_NEW,
            "request_id": request_id,
            "timestamp": utc_now(),
            "source": "bridge",
            "source_file": "",
            "normalized": for_ea,
            "signal": json.dumps(for_ea, separators=(",", ":"), default=str),
        }

    def request_account_refresh(self) -> Dict[str, Any]:
        """Ask for a snapshot on the EA's next poll, ignoring the age gate.

        Does not build the envelope here: the EA only ever receives a command by
        polling, so this sets a flag that the next ``ea_poll`` acts on. The board
        then sees the new snapshot arrive over SSE or its next state poll.
        """
        with self._lock:
            self._account_force = True
        return {"ok": True, "note": "an account snapshot will be requested on the EA's next poll"}

    def account_state(self) -> Dict[str, Any]:
        """The latest stored snapshot plus how fresh it is, for the API/board."""
        snapshot, at = self.store.load_account_snapshot()
        age = None
        if at:
            when = parse_iso(at)
            if when is not None:
                age = round(max(0.0, (datetime.now(timezone.utc) - when).total_seconds()), 1)
        with self._lock:
            in_flight = self._account_inflight_at is not None
        return {
            "snapshot": snapshot,
            "at": at,
            "age_seconds": age,
            "poll_seconds": self.settings.account_poll_seconds,
            "in_flight": in_flight,
        }

    def _handle_account_ack(self, body: Dict[str, Any], request_id: str) -> Dict[str, Any]:
        """Store an ``account`` snapshot ack. It is not a signal: no ledger row.

        The EA answers an account request with retcode 0 and a ``snapshot``
        object. There is no matching row in the signals table (the bridge never
        inserted one), so this path deliberately avoids record_result - which
        would be a no-op anyway - and keeps the snapshot out of the trade ledger.
        """
        snapshot = body.get("snapshot")
        stored = isinstance(snapshot, dict) and bool(snapshot)
        if stored:
            self.store.store_account_snapshot(snapshot)
        with self._lock:
            # Clear the outstanding flag only for the request we are waiting on,
            # so a late or duplicate ack cannot mask a still-pending ask.
            if request_id and request_id == self._account_request_id:
                self._account_inflight_at = None
        detail = {
            "stored": stored,
            "account": snapshot.get("account") if stored else None,
            "positions_count": snapshot.get("positions_count") if stored else None,
            "pending_orders_count": snapshot.get("pending_orders_count") if stored else None,
        }
        self.emit(events.ACCOUNT_SNAPSHOT, None, detail)
        return {"ok": True, "account": True, "request_id": request_id, "stored": stored}

    def ea_ack(self, body: Any) -> Dict[str, Any]:
        """Record the outcome Bot_Trader POSTs to ``ProcessedURL``.

        The EA reports every processed signal, including the ones its own guards
        skipped, so this is the authoritative answer to "did that trade happen".
        An ack that arrives after the bridge gave up waiting supersedes the
        expiry - the position is open whether or not we were still watching.
        """
        if isinstance(body, (bytes, bytearray)):
            try:
                body = json.loads(body.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                body = None
        if isinstance(body, str):
            try:
                body = json.loads(body)
            except json.JSONDecodeError:
                body = None
        if not isinstance(body, dict):
            self.emit(events.SIGNAL_RESULT, None, {"error": "acknowledgement was not a JSON object"})
            return {"ok": False, "error": "acknowledgement must be a JSON object"}

        request_id = str(body.get("request_id") or "").strip()
        if not request_id:
            self.emit(events.SIGNAL_RESULT, None, {"error": "acknowledgement carried no request_id", "body": body})
            return {"ok": False, "error": "request_id is required"}

        # An account snapshot is transient state, not a trade: it has no ledger
        # row and must never create one. Detected by the snapshot payload the EA
        # attaches, or by the action it echoes back.
        if isinstance(body.get("snapshot"), dict) or contract.normalize_action(body.get("action")) == "account":
            return self._handle_account_ack(body, request_id)

        row = self.store.get_signal(request_id)
        ack_status = str(body.get("status") or "").strip().upper()
        status = _ACK_STATUS.get(ack_status)
        if status is None:
            # An unrecognised status still carries a retcode; report it as a
            # failure rather than dropping the outcome on the floor.
            status = contract.STATUS_FAILED

        retcode = body.get("retcode")
        try:
            retcode = int(retcode) if retcode is not None else None
        except (TypeError, ValueError):
            retcode = None
        result_text = body.get("result")
        result_text = str(result_text) if result_text is not None else None

        superseded = row is not None and row["status"] == contract.STATUS_EXPIRED
        self.store.record_result(request_id, status=status, retcode=retcode, result_text=result_text)

        detail: Dict[str, Any] = {
            "status": status,
            "ack_status": ack_status,
            "retcode": retcode,
            "retcode_label": contract.describe_retcode(retcode) if retcode is not None else None,
            "result": result_text,
            "action": body.get("action"),
            "symbol": body.get("symbol"),
            "size": body.get("size"),
            "ea_timestamp": body.get("timestamp"),
            "known": row is not None,
        }
        if superseded:
            detail["superseded_expiry"] = True
        self.emit(events.SIGNAL_RESULT, request_id, detail)

        return {"ok": True, "request_id": request_id, "status": status, "known": row is not None}

    # ------------------------------------------------------ expiry / liveness
    def tick(self) -> None:
        """Housekeeping the watcher thread runs even when the EA is silent.

        Without this, a bridge whose EA has stopped polling would keep queued
        commands alive indefinitely and never notice the EA going offline -
        expiry would only happen on the next poll, which by definition is not
        coming.
        """
        self._expire_overdue()
        self.ea_state()

    def _expire_overdue(self) -> None:
        for request_id in self.store.expire_stale(self.settings.signal_ttl_seconds):
            self.emit(
                events.SIGNAL_EXPIRED,
                request_id,
                {"reason": f"queued longer than {self.settings.signal_ttl_seconds}s without dispatch"},
            )
        for request_id in self.store.expire_dispatched(self.settings.dispatch_timeout_seconds):
            self.emit(
                events.SIGNAL_EXPIRED,
                request_id,
                {
                    "reason": f"no acknowledgement from the EA within {self.settings.dispatch_timeout_seconds}s",
                    "note": "not re-sent: the EA dedups on the exact body, so a retry needs a new request_id",
                },
            )

    def _note_ea_seen(self) -> None:
        self.store.set_runtime("ea_last_seen", utc_now())
        self._set_ea_online(True)

    def _set_ea_online(self, online: bool) -> None:
        with self._lock:
            if self._ea_online == online:
                return
            self._ea_online = online
        self.store.set_runtime("ea_online", "1" if online else "0")
        self.emit(events.EA_ONLINE if online else events.EA_OFFLINE, None, {"seen_at": utc_now()})

    def ea_state(self) -> Dict[str, Any]:
        """Liveness of the EA, derived from when it last polled."""
        last_seen = self.store.get_runtime("ea_last_seen")
        seen_at = _parse_iso(last_seen)
        online = False
        seconds_ago = None
        if seen_at is not None:
            seconds_ago = max(0.0, (datetime.now(timezone.utc) - seen_at).total_seconds())
            online = seconds_ago <= EA_ONLINE_WINDOW_SECONDS
        # Called from the dashboard as well as from the poll loop, so a browser
        # left open on a dead bridge still reports the EA going offline.
        self._set_ea_online(online)
        return {
            "online": online,
            "last_seen_at": last_seen,
            "seconds_since_poll": round(seconds_ago, 1) if seconds_ago is not None else None,
            "online_window_seconds": EA_ONLINE_WINDOW_SECONDS,
        }

    # -------------------------------------------------------- board / control
    def snapshot(self, *, limit: int = 50, since_event: int = 0, metrics_window: int = 3600) -> Dict[str, Any]:
        """Everything the signal board needs in one round trip."""
        return {
            "now": utc_now(),
            "settings": self.settings.public_dict(),
            "editable_settings": mutable_view(),
            "ea": self.ea_state(),
            "queue_depth": self.store.queue_depth(),
            "account": self.account_state(),
            "metrics": self.store.metrics(metrics_window),
            "signals": [self._public_signal(s) for s in self.store.list_signals(limit=limit)],
            "events": self.store.list_events(since_id=since_event, limit=200),
            "watcher": self._watcher.status() if self._watcher is not None else None,
            "sse_clients": self.bus.client_count,
        }

    @staticmethod
    def _public_signal(row: Dict[str, Any]) -> Dict[str, Any]:
        out = dict(row)
        out.pop("raw_payload", None)
        out["command"] = public_command(out.get("command"))
        return out

    def signal_detail(self, request_id: str) -> Optional[Dict[str, Any]]:
        row = self.store.get_signal(request_id)
        if row is None:
            return None
        detail = self._public_signal(row)
        detail["events"] = self.store.list_events(request_id=request_id, limit=500)
        return detail

    def list_signals(self, **kwargs: Any) -> List[Dict[str, Any]]:
        return [self._public_signal(s) for s in self.store.list_signals(**kwargs)]

    def update_settings(self, updates: Dict[str, Any]) -> Tuple[Dict[str, Any], Dict[str, str]]:
        """Apply and persist runtime settings from the board.

        All-or-nothing: if any field is rejected, none of them change, so an
        operator never has to guess which half of a form took effect.
        """
        with self._lock:
            errors = self.settings.apply(updates, strict=True)
            if errors:
                return self.settings.public_dict(), errors
            accepted = sorted(updates)
            # apply() coerces and clamps, so persist the value that is actually
            # in force rather than the one the caller asked for.
            for key in accepted:
                self.store.set_setting(key, getattr(self.settings, key))
        self.emit(
            events.SETTINGS_CHANGED,
            None,
            {"changed": accepted, "settings": self.settings.public_dict()},
        )
        if self._watcher is not None:
            self._watcher.notify_settings_changed()
        return self.settings.public_dict(), {}

    def preview(self, payload: Any) -> Dict[str, Any]:
        """Normalize and validate without queueing anything.

        The signal board's test form and any producer that wants to check a
        command before committing it to a live account use this. It writes no
        ledger row, so it cannot affect what the EA is sent.
        """
        command = schema.normalize_command(payload)
        if "error" in command:
            return {
                "valid": False,
                "status": contract.STATUS_INVALID,
                "errors": [command["error"]],
                "warnings": [],
                "command": public_command(command),
                "summary": "",
            }
        report = validator.validate_command(command, self.settings.validation_mode)
        stale = self._journal_stale_error(command)
        if stale is not None:
            report = dict(report, valid=False, errors=list(report["errors"]) + [stale])
        warnings = list(report["warnings"]) + schema.shadowing_risk(command)
        return {
            "valid": report["valid"],
            "status": contract.STATUS_QUEUED if report["valid"] else contract.STATUS_REJECTED,
            "errors": report["errors"],
            "warnings": warnings,
            "command": public_command(command),
            "summary": schema.summarize(command),
            "mode": self.settings.validation_mode,
            "envelope": self._preview_envelope(command),
        }

    def _journal_stale_error(self, command: Dict[str, Any]) -> Optional[str]:
        """Refuse a journal entry whose trigger stamp is old.

        The journal's own ``lifecycle.triggered_at`` is the authority on when the
        setup fired; this only judges freshness, never the trade parameters. A
        record written at trigger time reaches the bridge within a second or two,
        so anything older than the limit is history sitting in a shared inbox -
        and history must not become a burst of market orders.
        """
        meta = command.get("_meta")
        if not isinstance(meta, dict) or not meta.get("is_journal"):
            return None
        limit = max(0, int(self.settings.journal_max_age_seconds))
        if limit == 0:
            return None
        when = parse_iso(command.get("timestamp"))
        if when is None:
            return None
        age = (datetime.now(timezone.utc) - when).total_seconds()
        if age <= limit:
            return None
        return (
            f"journal trigger {command.get('timestamp')} is {int(age)}s old, older than "
            f"journal_max_age_seconds ({limit}): a stale trigger is history, not an order"
        )

    def _preview_envelope(self, command: Dict[str, Any]) -> Dict[str, Any]:
        """The exact bytes the EA would receive, for on-screen comparison.

        Must mirror _build_envelope key for key, including source/source_file:
        a preview that differs from the real envelope would let the operator
        approve one thing and send another.
        """
        for_ea = public_command(command)
        return {
            "status": contract.ENVELOPE_NEW,
            "request_id": "<assigned at dispatch>",
            "timestamp": utc_now(),
            "source": contract.SOURCE_API,
            "source_file": "",
            "normalized": for_ea,
            "signal": json.dumps(for_ea, separators=(",", ":"), default=str),
        }

    def clear_queue(self, reason: str = "cleared by operator") -> Dict[str, Any]:
        """Cancel everything still waiting. Nothing already sent can be recalled."""
        cancelled = []
        while True:
            pending = self.store.next_queued(limit=100)
            if not pending:
                break
            for row in pending:
                if self.store.cancel_signal(row["request_id"]):
                    cancelled.append(row["request_id"])
            if len(pending) < 100:
                break
        for request_id in cancelled:
            self.emit(events.SIGNAL_CANCELLED, request_id, {"reason": reason})
        in_flight = len(self.store.list_signals(status=contract.STATUS_DISPATCHED, limit=1000))
        return {"ok": True, "cancelled": cancelled, "count": len(cancelled), "still_dispatched": in_flight}

    def cancel(self, request_id: str) -> Dict[str, Any]:
        row = self.store.get_signal(request_id)
        if row is None:
            return {"ok": False, "error": "unknown request_id"}
        if row["status"] != contract.STATUS_QUEUED:
            return {
                "ok": False,
                "error": f"only a queued signal can be cancelled (this one is {row['status']})",
                "status": row["status"],
            }
        self.store.cancel_signal(request_id)
        self.emit(events.SIGNAL_CANCELLED, request_id, {"summary": row.get("result_text") or ""})
        return {"ok": True, "status": contract.STATUS_CANCELLED}

    def retry(self, request_id: str) -> Dict[str, Any]:
        """Re-submit a finished command under a fresh request_id.

        A new id is required, not optional: Bot_Trader skips a payload identical
        to the last one it processed, so re-sending the same body would be
        silently ignored even when a retry is exactly what the operator wants.
        """
        row = self.store.get_signal(request_id)
        if row is None:
            return {"ok": False, "error": "unknown request_id"}
        if row["status"] in (contract.STATUS_QUEUED, contract.STATUS_DISPATCHED):
            return {"ok": False, "error": f"signal is already {row['status']}", "status": row["status"]}

        payload = public_command(row["command"] if isinstance(row["command"], dict) else {})
        payload.pop("request_id", None)
        result = self.submit(
            payload,
            row.get("source") or contract.SOURCE_MANUAL,
            source_file=row.get("source_file"),
        )
        result["retried_from"] = request_id
        return result

    def requeue(self, request_id: str) -> Dict[str, Any]:
        """Put an expired or cancelled signal back on the queue, same id."""
        row = self.store.get_signal(request_id)
        if row is None:
            return {"ok": False, "error": "unknown request_id"}
        if row["status"] not in (contract.STATUS_EXPIRED, contract.STATUS_CANCELLED):
            return {"ok": False, "error": f"cannot requeue a {row['status']} signal"}
        self.store.requeue_signal(request_id)
        self.emit(events.SIGNAL_REQUEUED, request_id, {"reason": "operator requeued"})
        return {"ok": True, "status": contract.STATUS_QUEUED}
