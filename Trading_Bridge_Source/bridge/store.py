"""SQLite persistence: the signal ledger, event timeline, and settings.

This is the durable record that makes the bridge safe to restart. Every
command that reaches the EA is written here before dispatch and updated with
the outcome after, so a signal is never sent twice and never silently
forgotten.

Design notes:

* **WAL mode** so the dashboard can read while the watcher and the HTTP
  threads write, without readers blocking writers.
* **Thread-local connections.** sqlite3 objects are not shareable across
  threads by default, and Waitress serves requests from a pool. Each thread
  gets its own connection, opened lazily.
* **Monotonic integer ids** double as the FIFO order and as the cursor for
  the dashboard's event polling, so no separate sequence is needed.
* **Schema versioning** via ``PRAGMA user_version`` so a future column can be
  added without discarding an operator's trade history.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from . import contract
from .util import to_ea_timestamp, utc_now

SCHEMA_VERSION = 3

# Timestamps are stored with SQLite's own datetime('now') so that every
# comparison against datetime('now', '-N seconds') and every strftime() bucket
# is a like-for-like text comparison. An ISO-8601 "…T…Z" string sorts
# incorrectly against SQLite's "YYYY-MM-DD HH:MM:SS" because 'T' > ' ', which
# would silently disable the dedup window and the TTL. The EA-facing ISO form
# is produced on the way out by util.to_ea_timestamp().
_NOW = "datetime('now')"

_LOCAL = threading.local()

_SCHEMA = """
CREATE TABLE IF NOT EXISTS signals (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    request_id     TEXT    NOT NULL UNIQUE,
    dedup_hash     TEXT    NOT NULL,
    source         TEXT    NOT NULL,
    source_file    TEXT,
    raw_payload    TEXT    NOT NULL,
    command        TEXT    NOT NULL,
    action         TEXT    NOT NULL,
    symbol         TEXT,
    size_value     REAL,
    size_unit      TEXT,
    status         TEXT    NOT NULL,
    warnings       TEXT    NOT NULL DEFAULT '[]',
    error          TEXT,
    retcode        INTEGER,
    result_text    TEXT,
    created_at     TEXT    NOT NULL,
    dispatched_at  TEXT,
    finished_at    TEXT,
    queue_ms       INTEGER,
    exec_ms        INTEGER
);

CREATE INDEX IF NOT EXISTS idx_signals_status  ON signals(status);
CREATE INDEX IF NOT EXISTS idx_signals_created ON signals(created_at);
CREATE INDEX IF NOT EXISTS idx_signals_hash    ON signals(dedup_hash, created_at);
CREATE INDEX IF NOT EXISTS idx_signals_symbol  ON signals(symbol);
CREATE INDEX IF NOT EXISTS idx_signals_action  ON signals(action);

CREATE TABLE IF NOT EXISTS events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    request_id  TEXT,
    type        TEXT NOT NULL,
    detail      TEXT,
    created_at  TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_events_request ON events(request_id);

CREATE TABLE IF NOT EXISTS settings (
    key        TEXT PRIMARY KEY,
    value      TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS runtime (
    key       TEXT PRIMARY KEY,
    value     TEXT NOT NULL
);

-- Register of inbox files the bridge has consumed. The inbox is read-only and
-- shared with other producers, so a handled file cannot be renamed to mark it;
-- this table is the memory instead, and it is permanent rather than windowed.
-- The UNIQUE pair is the claim: inserting it is what makes intake safe against
-- a second scanner, a restart, or the same file being seen again next tick.
CREATE TABLE IF NOT EXISTS file_intake (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    source_path  TEXT    NOT NULL,
    content_hash TEXT    NOT NULL,
    size         INTEGER,
    mtime_ns     INTEGER,
    request_id   TEXT,
    outcome      TEXT,
    copy_name    TEXT,
    claimed_at   TEXT    NOT NULL,
    resolved_at  TEXT,
    UNIQUE(source_path, content_hash)
);

CREATE INDEX IF NOT EXISTS idx_intake_outcome ON file_intake(outcome);
CREATE INDEX IF NOT EXISTS idx_intake_path    ON file_intake(source_path);

-- Files that were already in an inbox at the moment the bridge started watching
-- it, recorded by path so they are never traded. The inbox can be changed from
-- the board while the bridge runs; pointing it at a folder of history would
-- otherwise queue one trade per file. Keyed on path alone, not on content,
-- because a shared inbox is often written by an application that updates its own
-- records in place - and an in-place update must not look like a new command.
CREATE TABLE IF NOT EXISTS inbox_adoption (
    source_path TEXT PRIMARY KEY,
    folder      TEXT    NOT NULL,
    size        INTEGER,
    mtime_ns    INTEGER,
    adopted_at  TEXT    NOT NULL
);
"""

# Statuses that mean "the EA has this or had it" - a duplicate of one of these
# must not be dispatched again.
_IN_FLIGHT = (contract.STATUS_QUEUED, contract.STATUS_DISPATCHED)
# Statuses whose identical content is refused for a while after they finished.
# Only statuses where a trade may actually have happened belong here: executed
# obviously, and expired because the EA may have traded after we gave up
# waiting. failed and skipped are deliberately absent - the EA reported that no
# trade happened, so blocking an immediate retry would turn the board's Retry
# button into a no-op for the exact case it exists for.
_DEDUP_WINDOW = (contract.STATUS_EXECUTED, contract.STATUS_EXPIRED)

# Statuses where the command never reached the broker, so resubmitting the same
# request_id is a retry rather than a double execution. request_id is a UNIQUE
# column, so a retry revives the existing row instead of inserting a second one.
RETRYABLE_STATUSES: frozenset = frozenset({
    contract.STATUS_REJECTED,
    contract.STATUS_INVALID,
    contract.STATUS_EXPIRED,
    contract.STATUS_CANCELLED,
})


class DuplicateSignal(Exception):
    """Raised when intake would re-send a command already handled."""

    def __init__(self, reason: str, existing_id: Optional[str] = None) -> None:
        super().__init__(reason)
        self.reason = reason
        self.existing_id = existing_id


def _row_to_dict(row: sqlite3.Row) -> Dict[str, Any]:
    data = dict(row)
    for key in ("command", "raw_payload", "warnings"):
        if key in data and isinstance(data[key], str):
            try:
                data[key] = json.loads(data[key])
            except json.JSONDecodeError:
                pass
    return data


class Store:
    """Thread-safe SQLite access for one bridge process."""

    def __init__(self, db_path: Path) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_lock = threading.Lock()
        self._initialize()

    # ---------------------------------------------------------------- conn
    @property
    def _conn(self) -> sqlite3.Connection:
        conn = getattr(_LOCAL, "conn", None)
        path = getattr(_LOCAL, "path", None)
        if conn is None or path != str(self.db_path):
            if conn is not None:
                try:
                    conn.close()
                except sqlite3.Error:
                    pass
            conn = sqlite3.connect(str(self.db_path), timeout=30.0)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute("PRAGMA busy_timeout=30000")
            _LOCAL.conn = conn
            _LOCAL.path = str(self.db_path)
        return conn

    def _initialize(self) -> None:
        with self._init_lock:
            conn = self._conn
            conn.executescript(_SCHEMA)
            version = conn.execute("PRAGMA user_version").fetchone()[0]
            if version < SCHEMA_VERSION:
                conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
            conn.commit()

    def close(self) -> None:
        conn = getattr(_LOCAL, "conn", None)
        if conn is not None:
            conn.close()
            _LOCAL.conn = None
            _LOCAL.path = None

    # -------------------------------------------------------------- events
    def add_event(self, event_type: str, request_id: Optional[str] = None, detail: Any = None) -> int:
        text = detail if isinstance(detail, str) or detail is None else json.dumps(detail, default=str)
        cur = self._conn.execute(
            "INSERT INTO events (request_id, type, detail, created_at) VALUES (?,?,?,datetime('now'))",
            (request_id, event_type, text),
        )
        self._conn.commit()
        return int(cur.lastrowid)

    def list_events(self, since_id: int = 0, limit: int = 200, request_id: Optional[str] = None) -> List[Dict[str, Any]]:
        sql = "SELECT * FROM events WHERE id > ?"
        args: List[Any] = [since_id]
        if request_id is not None:
            sql += " AND request_id = ?"
            args.append(request_id)
        sql += " ORDER BY id ASC LIMIT ?"
        args.append(limit)
        return [_row_to_dict(r) for r in self._conn.execute(sql, args).fetchall()]

    # ------------------------------------------------------- intake register
    def claim_file(
        self,
        *,
        source_path: str,
        content_hash: str,
        size: Optional[int] = None,
        mtime_ns: Optional[int] = None,
    ) -> Optional[Dict[str, Any]]:
        """Claim an inbox file. Returns the existing row if it was already claimed.

        ``None`` means this caller won the claim and must go on to copy and
        submit the file. A returned row means the identical content at the same
        path was consumed before, so the file must be left alone: the inbox is
        read-only, so this table is the only thing standing between a file that
        stays in the folder forever and a second execution of the same command.
        """
        try:
            self._conn.execute(
                """INSERT INTO file_intake (source_path, content_hash, size, mtime_ns,
                                             outcome, claimed_at)
                   VALUES (?,?,?,?,?,datetime('now'))""",
                (source_path, content_hash, size, mtime_ns, contract.INTAKE_OUTCOME_CLAIMED),
            )
            self._conn.commit()
        except sqlite3.IntegrityError:
            self._conn.rollback()
            row = self._conn.execute(
                "SELECT * FROM file_intake WHERE source_path = ? AND content_hash = ?",
                (source_path, content_hash),
            ).fetchone()
            return _row_to_dict(row) if row is not None else {"outcome": "unknown"}
        return None

    def resolve_file_claim(
        self,
        *,
        source_path: str,
        content_hash: str,
        outcome: str,
        request_id: Optional[str] = None,
        copy_name: Optional[str] = None,
    ) -> None:
        self._conn.execute(
            """UPDATE file_intake
                  SET outcome = ?, request_id = COALESCE(?, request_id),
                      copy_name = COALESCE(?, copy_name), resolved_at = datetime('now')
                WHERE source_path = ? AND content_hash = ?""",
            (outcome, request_id, copy_name, source_path, content_hash),
        )
        self._conn.commit()

    def unresolved_file_claims(self) -> List[Dict[str, Any]]:
        """Claims that were opened but never resolved: the bridge died mid-intake.

        These are the read-only-inbox equivalent of a ``.processing`` file left
        behind by a crash. They are reported, never replayed - the command may
        already have reached the EA.
        """
        rows = self._conn.execute(
            "SELECT * FROM file_intake WHERE outcome IS NULL OR outcome = ? ORDER BY id ASC",
            (contract.INTAKE_OUTCOME_CLAIMED,),
        ).fetchall()
        return [_row_to_dict(r) for r in rows]

    def known_file_signatures(self) -> Dict[str, Tuple[int, int, str]]:
        """path -> (size, mtime_ns, content_hash) for everything already consumed.

        The watcher seeds its stat cache from this at startup so a folder full of
        files it handled last week is skipped without being read or hashed again.
        """
        rows = self._conn.execute(
            "SELECT source_path, size, mtime_ns, content_hash FROM file_intake"
        ).fetchall()
        out: Dict[str, Tuple[int, int, str]] = {}
        for row in rows:
            if row["size"] is None or row["mtime_ns"] is None:
                continue
            out[row["source_path"]] = (int(row["size"]), int(row["mtime_ns"]), row["content_hash"])
        return out

    def intake_counts(self) -> Dict[str, int]:
        rows = self._conn.execute(
            "SELECT COALESCE(outcome, ?) AS outcome, COUNT(*) AS n FROM file_intake GROUP BY outcome",
            (contract.INTAKE_OUTCOME_CLAIMED,),
        ).fetchall()
        counts = {str(r["outcome"]): int(r["n"]) for r in rows}
        counts["total"] = sum(counts.values())
        return counts

    # ------------------------------------------------------------- adoption
    def adopt_files(self, folder: str, entries: Sequence[Tuple[str, Optional[int], Optional[int]]]) -> int:
        """Record inbox files that predate our watching this folder.

        Idempotent: adopting the same path twice changes nothing. Returns how
        many paths were newly adopted.
        """
        added = 0
        for source_path, size, mtime_ns in entries:
            cursor = self._conn.execute(
                """INSERT OR IGNORE INTO inbox_adoption
                       (source_path, folder, size, mtime_ns, adopted_at)
                   VALUES (?,?,?,?,datetime('now'))""",
                (source_path, folder, size, mtime_ns),
            )
            added += cursor.rowcount or 0
        self._conn.commit()
        return added

    def adopted_paths(self) -> set:
        """Every path the bridge has promised itself never to trade."""
        return {row["source_path"] for row in self._conn.execute("SELECT source_path FROM inbox_adoption")}

    def adoption_count(self) -> int:
        row = self._conn.execute("SELECT COUNT(*) AS n FROM inbox_adoption").fetchone()
        return int(row["n"]) if row else 0

    # ------------------------------------------------------------- signals
    def find_duplicate(
        self, dedup_hash: str, request_id: str, dedup_window_seconds: int
    ) -> Optional[Dict[str, Any]]:
        """Return the conflicting row when this command was already handled.

        Two distinct rules:

        * An explicit ``request_id`` is an idempotency key - reusing one is
          always a duplicate, however old. The caller decides what to do with
          it: if the existing row is in RETRYABLE_STATUSES the command never
          reached the broker, so it can be revived rather than refused.
        * Otherwise, content equality only counts as a duplicate while the
          command is in flight, or within the dedup window after it finished.
          Beyond the window a strategy may legitimately re-enter the same way.
        """
        row = self._conn.execute(
            "SELECT * FROM signals WHERE request_id = ?", (request_id,)
        ).fetchone()
        if row is not None:
            return _row_to_dict(row)

        row = self._conn.execute(
            "SELECT * FROM signals WHERE dedup_hash = ? AND status IN (%s) "
            "ORDER BY id DESC LIMIT 1" % ",".join("?" * len(_IN_FLIGHT)),
            (dedup_hash, *_IN_FLIGHT),
        ).fetchone()
        if row is not None:
            return _row_to_dict(row)

        if dedup_window_seconds > 0:
            row = self._conn.execute(
                "SELECT * FROM signals WHERE dedup_hash = ? AND status IN (%s) "
                "AND created_at >= datetime('now', ?) ORDER BY id DESC LIMIT 1"
                % ",".join("?" * len(_DEDUP_WINDOW)),
                (dedup_hash, *_DEDUP_WINDOW, f"-{int(dedup_window_seconds)} seconds"),
            ).fetchone()
            if row is not None:
                return _row_to_dict(row)
        return None

    def insert_signal(
        self,
        *,
        request_id: str,
        dedup_hash: str,
        source: str,
        command: Dict[str, Any],
        raw_payload: Any,
        status: str,
        warnings: Sequence[str] = (),
        error: Optional[str] = None,
        source_file: Optional[str] = None,
    ) -> int:
        size = command.get("size") if isinstance(command, dict) else None
        size_value = size.get("value") if isinstance(size, dict) else size
        if not isinstance(size_value, (int, float)):
            size_value = None
        size_unit = size.get("unit") if isinstance(size, dict) else "lots"

        cur = self._conn.execute(
            """INSERT INTO signals
               (request_id, dedup_hash, source, source_file, raw_payload, command,
                action, symbol, size_value, size_unit, status, warnings, error, created_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,datetime('now'))""",
            (
                request_id,
                dedup_hash,
                source,
                source_file,
                json.dumps(raw_payload, default=str),
                json.dumps(command, default=str),
                str(command.get("action", "")) if isinstance(command, dict) else "",
                command.get("symbol") if isinstance(command, dict) else None,
                size_value,
                size_unit,
                status,
                json.dumps(list(warnings)),
                error,
            ),
        )
        self._conn.commit()
        return int(cur.lastrowid)

    def set_status(
        self,
        request_id: str,
        status: str,
        *,
        error: Optional[str] = None,
        retcode: Optional[int] = None,
        result_text: Optional[str] = None,
        stamp_dispatched: bool = False,
        stamp_finished: bool = False,
    ) -> bool:
        sets = ["status = ?"]
        args: List[Any] = [status]
        if error is not None:
            sets.append("error = ?")
            args.append(error)
        if retcode is not None:
            sets.append("retcode = ?")
            args.append(retcode)
        if result_text is not None:
            sets.append("result_text = ?")
            args.append(result_text)
        if stamp_dispatched:
            # SET expressions read the pre-update row, so the latency is
            # computed from datetime('now') rather than the column just written.
            sets.append("dispatched_at = datetime('now')")
            sets.append(
                "queue_ms = CAST((julianday(datetime('now')) - julianday(created_at)) * 86400000 AS INTEGER)"
            )
        if stamp_finished:
            sets.append("finished_at = datetime('now')")
            sets.append(
                "exec_ms = CAST((julianday(datetime('now')) - "
                "julianday(COALESCE(dispatched_at, created_at))) * 86400000 AS INTEGER)"
            )
        args.append(request_id)
        cur = self._conn.execute(
            f"UPDATE signals SET {', '.join(sets)} WHERE request_id = ?", args
        )
        self._conn.commit()
        return cur.rowcount > 0

    def get_signal(self, request_id: str) -> Optional[Dict[str, Any]]:
        row = self._conn.execute("SELECT * FROM signals WHERE request_id = ?", (request_id,)).fetchone()
        return _row_to_dict(row) if row else None

    def next_queued(self, limit: int = 1) -> List[Dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT * FROM signals WHERE status = ? ORDER BY id ASC LIMIT ?",
            (contract.STATUS_QUEUED, limit),
        ).fetchall()
        return [_row_to_dict(r) for r in rows]

    def claim_next(self) -> Optional[Dict[str, Any]]:
        """Atomically take the oldest queued command and mark it dispatched.

        The claim is a conditional UPDATE rather than a read-then-write, because
        Waitress serves the EA's poll from a thread pool: two concurrent polls
        must never be handed the same command, or one signal becomes two trades.
        """
        for _ in range(5):
            row = self._conn.execute(
                "SELECT request_id FROM signals WHERE status = ? ORDER BY id ASC LIMIT 1",
                (contract.STATUS_QUEUED,),
            ).fetchone()
            if row is None:
                return None
            request_id = row["request_id"]
            cur = self._conn.execute(
                "UPDATE signals SET status = ?, dispatched_at = datetime('now'), "
                "queue_ms = CAST((julianday(datetime('now')) - julianday(created_at)) * 86400000 AS INTEGER) "
                "WHERE request_id = ? AND status = ?",
                (contract.STATUS_DISPATCHED, request_id, contract.STATUS_QUEUED),
            )
            self._conn.commit()
            if cur.rowcount:
                return self.get_signal(request_id)
            # Another thread won this row; take the next one.
        return None

    def expire_stale(self, ttl_seconds: int) -> List[str]:
        """Move queued commands older than the TTL out of the queue.

        Returns their request_ids so the caller can log and event them. Without
        this, a bridge that was down overnight would hand the EA a pile of
        entries at the first poll after restart.
        """
        if ttl_seconds <= 0:
            return []
        rows = self._conn.execute(
            "SELECT request_id FROM signals WHERE status = ? AND created_at < datetime('now', ?)",
            (contract.STATUS_QUEUED, f"-{int(ttl_seconds)} seconds"),
        ).fetchall()
        ids = [r["request_id"] for r in rows]
        if ids:
            self._conn.execute(
                "UPDATE signals SET status = ?, finished_at = datetime('now'), "
                "error = COALESCE(error, ?) WHERE request_id IN (%s)" % ",".join("?" * len(ids)),
                (contract.STATUS_EXPIRED, "expired before dispatch", *ids),
            )
            self._conn.commit()
        return ids

    def cancel_signal(self, request_id: str) -> bool:
        return self.set_status(request_id, contract.STATUS_CANCELLED, error="cancelled by operator", stamp_finished=True)

    def requeue_signal(self, request_id: str) -> bool:
        """Put a finished signal back on the queue, bypassing dedup."""
        cur = self._conn.execute(
            "UPDATE signals SET status = ?, error = NULL, retcode = NULL, result_text = NULL, "
            "dispatched_at = NULL, finished_at = NULL, queue_ms = NULL, exec_ms = NULL "
            "WHERE request_id = ?",
            (contract.STATUS_QUEUED, request_id),
        )
        self._conn.commit()
        return cur.rowcount > 0

    def revive_signal(
        self,
        request_id: str,
        *,
        command: Dict[str, Any],
        raw_payload: Any,
        dedup_hash: str,
        warnings: Sequence[str] = (),
        error: Optional[str] = None,
    ) -> bool:
        """Re-submit a retryable request_id with a freshly normalized command.

        A rejected or expired signal keeps its row, so the ledger still shows
        every attempt. Only rows in RETRYABLE_STATUSES can be revived - an
        executed one must never be overwritten, or the board would lose the
        record of a trade that actually happened.
        """
        size = command.get("size")
        size_value = size.get("value") if isinstance(size, dict) else size
        if not isinstance(size_value, (int, float)):
            size_value = None
        size_unit = size.get("unit") if isinstance(size, dict) else "lots"

        cur = self._conn.execute(
            """UPDATE signals SET
                   dedup_hash = ?, raw_payload = ?, command = ?, action = ?, symbol = ?,
                   size_value = ?, size_unit = ?, status = ?, warnings = ?, error = ?,
                   retcode = NULL, result_text = NULL, dispatched_at = NULL,
                   finished_at = NULL, queue_ms = NULL, exec_ms = NULL
               WHERE request_id = ? AND status IN (%s)"""
            % ",".join("?" * len(RETRYABLE_STATUSES)),
            (
                dedup_hash,
                json.dumps(raw_payload, default=str),
                json.dumps(command, default=str),
                str(command.get("action", "")),
                command.get("symbol"),
                size_value,
                size_unit,
                contract.STATUS_QUEUED if error is None else contract.STATUS_REJECTED,
                json.dumps(list(warnings)),
                error,
                request_id,
                *RETRYABLE_STATUSES,
            ),
        )
        self._conn.commit()
        return cur.rowcount > 0

    def expire_dispatched(self, timeout_seconds: int) -> List[str]:
        """Mark unacknowledged dispatches expired; returns their request_ids.

        Bot_Trader posts an ack for every signal it processes, inside the same
        poll that executed it. A dispatch older than the timeout therefore means
        the EA stopped polling or crashed mid-trade. The board needs to show
        that instead of leaving the row "dispatched" forever - but the command
        is deliberately not re-sent, because the EA dedups on the exact body
        string and a second execution is worse than a missed trade.
        """
        if timeout_seconds <= 0:
            return []
        rows = self._conn.execute(
            "SELECT request_id FROM signals WHERE status = ? AND dispatched_at < datetime('now', ?)",
            (contract.STATUS_DISPATCHED, f"-{int(timeout_seconds)} seconds"),
        ).fetchall()
        ids = [r["request_id"] for r in rows]
        if ids:
            self._conn.execute(
                "UPDATE signals SET status = ?, finished_at = datetime('now'), "
                "error = ? WHERE request_id IN (%s)" % ",".join("?" * len(ids)),
                (
                    contract.STATUS_EXPIRED,
                    f"no acknowledgement from the EA within {int(timeout_seconds)}s",
                    *ids,
                ),
            )
            self._conn.commit()
        return ids

    def record_result(
        self,
        request_id: str,
        *,
        status: str,
        retcode: Optional[int],
        result_text: Optional[str],
    ) -> bool:
        """Store the EA's ReportSignalResult ack."""
        return self.set_status(
            request_id,
            status,
            retcode=retcode,
            result_text=result_text,
            stamp_finished=True,
        )

    def list_signals(
        self,
        *,
        limit: int = 100,
        offset: int = 0,
        status: Optional[str] = None,
        symbol: Optional[str] = None,
        action: Optional[str] = None,
        search: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        sql = "SELECT * FROM signals WHERE 1=1"
        args: List[Any] = []
        if status:
            sql += " AND status = ?"
            args.append(status)
        if symbol:
            sql += " AND symbol = ?"
            args.append(symbol)
        if action:
            sql += " AND action = ?"
            args.append(action)
        if search:
            sql += " AND (request_id LIKE ? OR symbol LIKE ? OR result_text LIKE ?)"
            like = f"%{search}%"
            args.extend([like, like, like])
        sql += " ORDER BY id DESC LIMIT ? OFFSET ?"
        args.extend([limit, offset])
        return [_row_to_dict(r) for r in self._conn.execute(sql, args).fetchall()]

    def queue_depth(self) -> int:
        row = self._conn.execute(
            "SELECT COUNT(*) FROM signals WHERE status IN (?, ?)", _IN_FLIGHT
        ).fetchone()
        return int(row[0])

    def distinct_values(self, column: str) -> List[str]:
        if column not in ("symbol", "action", "status", "source"):
            return []
        rows = self._conn.execute(
            f"SELECT DISTINCT {column} AS v FROM signals WHERE {column} IS NOT NULL ORDER BY v"
        ).fetchall()
        return [r["v"] for r in rows]

    # ------------------------------------------------------------- settings
    def get_setting(self, key: str, default: Any = None) -> Any:
        row = self._conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
        if row is None:
            return default
        try:
            return json.loads(row["value"])
        except json.JSONDecodeError:
            return row["value"]

    def set_setting(self, key: str, value: Any) -> None:
        self._conn.execute(
            "INSERT INTO settings (key, value, updated_at) VALUES (?,?,datetime('now')) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
            (key, json.dumps(value, default=str)),
        )
        self._conn.commit()

    def load_settings(self, keys: Iterable[str]) -> Dict[str, Any]:
        out: Dict[str, Any] = {}
        for key in keys:
            value = self.get_setting(key)
            if value is not None:
                out[key] = value
        return out

    # -------------------------------------------------------------- runtime
    def set_runtime(self, key: str, value: str) -> None:
        self._conn.execute(
            "INSERT INTO runtime (key, value) VALUES (?,?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )
        self._conn.commit()

    def get_runtime(self, key: str) -> Optional[str]:
        row = self._conn.execute("SELECT value FROM runtime WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else None

    # ------------------------------------------------------ account snapshot
    # The EA's "account" action returns a full snapshot of the account, its open
    # positions and its pending orders. It is transient state, not a signal, so
    # it lives in the runtime KV table under one key rather than in the ledger:
    # a snapshot is overwritten wholesale and carries no history the operator
    # needs to audit. Stored verbatim - the bridge never reinterprets a value.
    def store_account_snapshot(self, snapshot: Dict[str, Any]) -> None:
        self.set_runtime("account_snapshot", json.dumps(snapshot, default=str))
        self.set_runtime("account_snapshot_at", utc_now())

    def load_account_snapshot(self) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
        raw = self.get_runtime("account_snapshot")
        if not raw:
            return None, None
        try:
            snapshot = json.loads(raw)
        except json.JSONDecodeError:
            return None, None
        if not isinstance(snapshot, dict):
            return None, None
        return snapshot, self.get_runtime("account_snapshot_at")

    # -------------------------------------------------------------- metrics
    def metrics(self, since_seconds: int = 3600) -> Dict[str, Any]:
        """Aggregates for the signal board, over a recent window and all time."""
        conn = self._conn
        cutoff = f"-{int(since_seconds)} seconds"

        by_status = {
            r["status"]: r["n"]
            for r in conn.execute("SELECT status, COUNT(*) AS n FROM signals GROUP BY status")
        }
        by_status_recent = {
            r["status"]: r["n"]
            for r in conn.execute(
                "SELECT status, COUNT(*) AS n FROM signals WHERE created_at >= datetime('now', ?) GROUP BY status",
                (cutoff,),
            )
        }
        by_action = [
            {"action": r["action"], "count": r["n"]}
            for r in conn.execute(
                "SELECT action, COUNT(*) AS n FROM signals GROUP BY action ORDER BY n DESC LIMIT 20"
            )
        ]
        by_symbol = [
            {"symbol": r["symbol"], "count": r["n"]}
            for r in conn.execute(
                "SELECT symbol, COUNT(*) AS n FROM signals WHERE symbol IS NOT NULL "
                "GROUP BY symbol ORDER BY n DESC LIMIT 20"
            )
        ]
        by_retcode = [
            {"retcode": r["retcode"], "count": r["n"], "label": contract.describe_retcode(r["retcode"])}
            for r in conn.execute(
                "SELECT retcode, COUNT(*) AS n FROM signals WHERE retcode IS NOT NULL "
                "GROUP BY retcode ORDER BY n DESC"
            )
        ]
        by_source = {
            r["source"]: r["n"]
            for r in conn.execute("SELECT source, COUNT(*) AS n FROM signals GROUP BY source")
        }

        latency = conn.execute(
            "SELECT COUNT(*) AS n, AVG(queue_ms) AS avg_queue, AVG(exec_ms) AS avg_exec, "
            "MAX(queue_ms) AS max_queue, MAX(exec_ms) AS max_exec FROM signals "
            "WHERE queue_ms IS NOT NULL"
        ).fetchone()

        throughput = [
            {"bucket": r["bucket"], "count": r["n"]}
            for r in conn.execute(
                "SELECT strftime('%Y-%m-%dT%H:%M', created_at) AS bucket, COUNT(*) AS n "
                "FROM signals WHERE created_at >= datetime('now', ?) "
                "GROUP BY bucket ORDER BY bucket ASC LIMIT 240",
                (cutoff,),
            )
        ]

        first = conn.execute("SELECT MIN(created_at) AS t FROM signals").fetchone()
        last = conn.execute("SELECT MAX(created_at) AS t FROM signals").fetchone()

        return {
            "window_seconds": since_seconds,
            "totals": {
                "all_time": sum(by_status.values()),
                "recent": sum(by_status_recent.values()),
                "by_status": by_status,
                "by_status_recent": by_status_recent,
                "by_source": by_source,
                "queue_depth": self.queue_depth(),
            },
            "by_action": by_action,
            "by_symbol": by_symbol,
            "by_retcode": by_retcode,
            "latency_ms": {
                "samples": latency["n"] or 0,
                "avg_queue": round(latency["avg_queue"] or 0, 1),
                "avg_exec": round(latency["avg_exec"] or 0, 1),
                "max_queue": latency["max_queue"],
                "max_exec": latency["max_exec"],
            },
            "throughput": throughput,
            "first_signal_at": first["t"] if first else None,
            "last_signal_at": last["t"] if last else None,
        }
