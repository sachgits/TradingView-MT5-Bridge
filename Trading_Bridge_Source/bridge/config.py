"""Runtime configuration.

Values come from environment variables at startup and a subset can be changed
while the bridge is running, either from the signal board or via
``PATCH /api/v1/settings``. Runtime changes are persisted to SQLite so a
restart does not silently revert the operator's tuning.

``MUTABLE`` declares exactly which fields are runtime-editable, with their
type and bounds. The settings endpoint validates against this table, so a
typo or an out-of-range value is rejected there rather than causing a
surprise later.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

from . import contract

ENV_PREFIX = "TRADING_BRIDGE_"

DEFAULT_SIGNALS_FOLDER = r"C:\Trades\trade_journal"
DEFAULT_DATA_FOLDERNAME = "TradingBridge"
DEFAULT_DB_FILENAME = "bridge.sqlite3"


def _env(name: str, default: str) -> str:
    return os.environ.get(ENV_PREFIX + name, default)


def _env_float(name: str, default: float) -> float:
    try:
        return float(_env(name, str(default)))
    except ValueError:
        return default


def _env_int(name: str, default: int) -> int:
    try:
        return int(_env_float(name, float(default)))
    except ValueError:
        return default


def _env_bool(name: str, default: bool) -> bool:
    return _env(name, "1" if default else "0").strip().lower() in {"1", "true", "yes", "on"}


@dataclass
class Settings:
    # --- intake -----------------------------------------------------------
    # The inbox is shared with other producers and is treated as READ-ONLY: the
    # bridge lists, stats and reads it and never renames, writes or deletes
    # anything in it. Everything the bridge owns lives under data_folder.
    signals_folder: Path = field(default_factory=lambda: Path(_env("SIGNALS_FOLDER", DEFAULT_SIGNALS_FOLDER)))
    data_folder: Path = field(default_factory=lambda: Path(_env("DATA_FOLDER", "")) if _env("DATA_FOLDER", "") else None)
    watch_interval: float = field(default_factory=lambda: _env_float("WATCH_INTERVAL", 1.0))
    # A producer writing foo.json directly can be read mid-write. The watcher
    # skips files whose size and mtime changed within this window, so a file is
    # only read once it has stopped growing.
    file_settle_ms: int = field(default_factory=lambda: _env_int("FILE_SETTLE_MS", 300))
    enable_file_intake: bool = field(default_factory=lambda: _env_bool("FILE_INTAKE", True))
    # A trade-journal record is a *now* signal: its producer writes it when the
    # setup triggers, and the bridge reads it within a second or two. A record
    # whose trigger stamp is older than this is history somebody left in the
    # inbox, and an inbox full of history must not become a burst of market
    # orders. Refusing is visible on the board; trading stale triggers is not
    # recoverable. 0 disables the check.
    journal_max_age_seconds: int = field(default_factory=lambda: _env_int("JOURNAL_MAX_AGE_SECONDS", 3600))

    # --- validation -------------------------------------------------------
    # "strict" requires an entry to state its own symbol and size; "permissive"
    # lets the EA's inputs supply them, matching raw Bot_Trader behaviour.
    validation_mode: str = field(default_factory=lambda: _env("VALIDATION_MODE", "strict"))

    # --- queue ------------------------------------------------------------
    max_queue_depth: int = field(default_factory=lambda: _env_int("MAX_QUEUE_DEPTH", 64))
    # A queued command older than this is expired rather than executed, so a
    # bridge that was offline overnight does not fire stale entries at open.
    signal_ttl_seconds: int = field(default_factory=lambda: _env_int("SIGNAL_TTL_SECONDS", 120))
    # An identical command within this window is treated as a duplicate. Long
    # enough to absorb DOM-reflection repeats, short enough that a strategy
    # legitimately re-entering the same way later still gets through.
    dedup_window_seconds: int = field(default_factory=lambda: _env_int("DEDUP_WINDOW_SECONDS", 10))

    # --- dispatch ---------------------------------------------------------
    # How long a command may sit in the EA's hands before the board stops
    # calling it in flight. Bot_Trader acks within one poll interval, so a
    # longer silence means the EA went offline or rejected the request without
    # answering. The bridge does NOT re-send: the EA dedups on the exact body,
    # and a duplicate execution is worse than a missed trade.
    dispatch_timeout_seconds: int = field(
        default_factory=lambda: _env_int("DISPATCH_TIMEOUT_SECONDS", 30)
    )
    paused: bool = field(default_factory=lambda: _env_bool("PAUSED", False))

    # --- account snapshot -------------------------------------------------
    # How often the bridge asks the EA for a read-only account/positions
    # snapshot, in seconds. The request is injected only when the trade queue is
    # empty, so it can never delay a real order, and the "account" action places
    # nothing - it works even when trading is disabled or the market is closed.
    # 0 disables automatic polling (the board can still request one on demand).
    account_poll_seconds: int = field(default_factory=lambda: _env_int("ACCOUNT_POLL_SECONDS", 30))

    # --- server -----------------------------------------------------------
    host: str = field(default_factory=lambda: _env("HOST", "127.0.0.1"))
    port: int = field(default_factory=lambda: _env_int("PORT", 8080))
    db_path: Path = field(default_factory=lambda: Path(_env("DB_PATH", "")) if _env("DB_PATH", "") else None)

    # --- security ---------------------------------------------------------
    # The bridge binds to loopback, but loopback is not a trust boundary: any
    # web page the operator visits can POST here, and any local process can
    # reach it. Rejecting cross-origin intake closes that.
    require_origin: bool = field(default_factory=lambda: _env_bool("REQUIRE_ORIGIN", True))
    allowed_origins: Tuple[str, ...] = field(
        default_factory=lambda: tuple(
            o.strip()
            for o in _env("ALLOWED_ORIGINS", "https://www.tradingview.com,https://tradingview.com").split(",")
            if o.strip()
        )
    )
    # Optional shared secret. When set, intake requests must send it in the
    # X-Bridge-Token header. Empty means token auth is off.
    intake_token: str = field(default_factory=lambda: _env("INTAKE_TOKEN", ""))

    def __post_init__(self) -> None:
        if self.data_folder is None:
            # Beside the inbox, not inside it: the bridge must never write to the
            # folder other producers share.
            self.data_folder = self.signals_folder.parent / DEFAULT_DATA_FOLDERNAME
        if self.db_path is None:
            self.db_path = self.data_folder / DEFAULT_DB_FILENAME
        # Names of settings that were given explicitly on the command line, and
        # so must not be overwritten by the value persisted in the ledger. Not a
        # dataclass field: it is startup plumbing, not part of the public shape.
        self.pinned: set = set()
        self.clamp()

    # --- derived paths ----------------------------------------------------
    @property
    def intake_dir(self) -> Path:
        """Where the bridge keeps its own copy of every file it consumed."""
        return self.data_folder / contract.INTAKE_SUBFOLDER

    @property
    def interrupted_dir(self) -> Path:
        """Copies whose claim was never resolved, set aside for an operator."""
        return self.data_folder / contract.INTAKE_INTERRUPTED_SUBFOLDER

    def ensure_dirs(self) -> None:
        """Create the folders the bridge owns. The inbox is created only if it is
        missing, and a failure to create it is not fatal: a read-only mount is a
        valid inbox."""
        self.data_folder.mkdir(parents=True, exist_ok=True)
        self.intake_dir.mkdir(parents=True, exist_ok=True)
        self.interrupted_dir.mkdir(parents=True, exist_ok=True)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            self.signals_folder.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass

    def path_conflict(self, inbox: Optional[Path] = None) -> Optional[str]:
        """Explain why these two folders cannot be used together, or None.

        The data folder inside the inbox would break the read-only promise and
        could feed the bridge its own copies back as new commands. ``inbox``
        lets a candidate be checked before it is accepted.
        """
        try:
            inbox = Path(inbox if inbox is not None else self.signals_folder).resolve()
            data = self.data_folder.resolve()
        except OSError as exc:
            return f"cannot resolve folder paths: {exc}"
        if inbox == data:
            return (
                "the data folder cannot be the watch folder: the bridge copies "
                "and renames files it owns, and must never write to the shared inbox"
            )
        if inbox in data.parents:
            return (
                f"the data folder {data} is inside the watch folder {inbox}: "
                "the bridge would write into the shared inbox"
            )
        if data in inbox.parents:
            return (
                f"the watch folder {inbox} is inside the data folder {data}: "
                "the bridge would treat its own working files as incoming commands"
            )
        return None

    # --- runtime mutation -------------------------------------------------
    def inbox_problem(self, text: str) -> Optional[str]:
        """Why this string cannot become the inbox, or None if it can.

        Checked before the value is staged, so a bad path from the board is
        rejected with a reason instead of pointing the watcher somewhere it
        should not read.
        """
        value = (text or "").strip().strip('"')
        if not value:
            return "a folder path is required"
        candidate = Path(value)
        if not candidate.is_absolute():
            return f"must be an absolute path, for example {DEFAULT_SIGNALS_FOLDER}"
        if candidate.exists() and not candidate.is_dir():
            return f"{candidate} is a file, not a folder"
        return self.path_conflict(candidate)

    def clamp(self) -> None:
        """Keep values inside sane bounds after any mutation."""
        self.watch_interval = min(max(self.watch_interval, 0.1), 3600.0)
        self.file_settle_ms = int(min(max(self.file_settle_ms, 0), 60_000))
        self.journal_max_age_seconds = int(min(max(self.journal_max_age_seconds, 0), 604_800))
        self.max_queue_depth = int(min(max(self.max_queue_depth, 1), 10_000))
        self.signal_ttl_seconds = int(min(max(self.signal_ttl_seconds, 5), 86_400))
        self.dedup_window_seconds = int(min(max(self.dedup_window_seconds, 0), 86_400))
        self.dispatch_timeout_seconds = int(min(max(self.dispatch_timeout_seconds, 5), 3_600))
        self.account_poll_seconds = int(min(max(self.account_poll_seconds, 0), 3_600))
        self.port = int(min(max(self.port, 1), 65_535))
        if self.validation_mode not in ("strict", "permissive"):
            self.validation_mode = "strict"

    def to_dict(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {}
        for f in fields(self):
            value = getattr(self, f.name)
            out[f.name] = str(value) if isinstance(value, Path) else (
                list(value) if isinstance(value, tuple) else value
            )
        return out

    def public_dict(self) -> Dict[str, Any]:
        """Settings safe to show in a browser. The token is never exposed."""
        data = self.to_dict()
        data["intake_token"] = "***" if self.intake_token else ""
        data["token_auth_enabled"] = bool(self.intake_token)
        return data

    def apply(self, updates: Dict[str, Any], strict: bool = False) -> Dict[str, str]:
        """Apply runtime updates, returning {field: error} for anything invalid.

        Only fields declared in MUTABLE can change at runtime. The data folder,
        ports and credentials are fixed at startup because changing them means
        moving the ledger or rebinding sockets. The inbox is the one path that
        can move while running: the bridge only reads it, so switching costs
        nothing but a different folder to list.

        With ``strict``, one bad field rejects the whole batch and nothing is
        changed. That is what the settings endpoint wants: an operator who
        submits a form containing a typo should not find that half of it took
        effect. Restoring persisted settings after a restart uses the default
        lenient behaviour instead, so one value corrupted by an older build
        cannot discard the operator's other tuning.
        """
        errors: Dict[str, str] = {}
        staged: Dict[str, Any] = {}

        for key, value in updates.items():
            spec = MUTABLE.get(key)
            if spec is None:
                errors[key] = (
                    "not runtime-editable"
                    if key in {f.name for f in fields(self)}
                    else "unknown setting"
                )
                continue
            kind, lo, hi = spec["type"], spec["min"], spec["max"]
            try:
                if kind is bool:
                    if isinstance(value, str):
                        if value.strip().lower() not in {"0", "1", "true", "false", "yes", "no", "on", "off"}:
                            raise ValueError(value)
                        coerced = value.strip().lower() in {"1", "true", "yes", "on"}
                    else:
                        coerced = bool(value)
                elif kind is float:
                    coerced = float(value)
                elif kind is int:
                    coerced = int(float(value))
                elif kind is str:
                    coerced = str(value)
                    if spec.get("path"):
                        problem = self.inbox_problem(coerced)
                        if problem:
                            errors[key] = problem
                            continue
                        coerced = Path(coerced.strip().strip('"'))
                    else:
                        choices = spec.get("choices")
                        if choices and coerced not in choices:
                            errors[key] = f"must be one of: {', '.join(choices)}"
                            continue
                else:
                    errors[key] = "unsupported setting type"
                    continue
            except (TypeError, ValueError):
                errors[key] = f"must be a {kind.__name__}"
                continue

            if lo is not None and coerced < lo:
                errors[key] = f"must be at least {lo}"
                continue
            if hi is not None and coerced > hi:
                errors[key] = f"must be at most {hi}"
                continue
            staged[key] = coerced

        if errors and strict:
            return errors

        for key, value in staged.items():
            setattr(self, key, value)
        self.clamp()
        return errors


# field -> {type, min, max, choices, label, help}
MUTABLE: Dict[str, Dict[str, Any]] = {
    "signals_folder": {
        "type": str, "min": None, "max": None, "path": True,
        "label": "Inbox folder (read-only)",
        "help": "Where producers drop .json commands. The bridge only reads it. Files already "
                "sitting there when you switch are adopted - recorded and never traded - so "
                "pointing the bridge at a folder of history cannot fire a trade per file.",
    },
    "watch_interval": {
        "type": float, "min": 0.1, "max": 3600.0,
        "label": "Watch interval (seconds)",
        "help": "How often the folder is scanned for new .json commands.",
    },
    "file_settle_ms": {
        "type": int, "min": 0, "max": 60_000,
        "label": "File settle window (ms)",
        "help": "Ignore files modified more recently than this, so a producer still writing is not read mid-write.",
    },
    "enable_file_intake": {
        "type": bool, "min": None, "max": None,
        "label": "File intake enabled",
        "help": "Turn off to accept commands over HTTP only.",
    },
    "journal_max_age_seconds": {
        "type": int, "min": 0, "max": 604_800,
        "label": "Journal trigger max age (seconds)",
        "help": "A trade-journal entry whose trigger stamp is older than this is refused as history rather than traded, so an inbox of old records cannot fire a burst of orders. 0 disables the check.",
    },
    "validation_mode": {
        "type": str, "min": None, "max": None, "choices": ("strict", "permissive"),
        "label": "Validation mode",
        "help": "strict requires an entry to state its own symbol and size; permissive lets the EA's inputs supply them.",
    },
    "max_queue_depth": {
        "type": int, "min": 1, "max": 10_000,
        "label": "Max queue depth",
        "help": "New commands are rejected once this many are waiting, instead of growing without bound.",
    },
    "signal_ttl_seconds": {
        "type": int, "min": 5, "max": 86_400,
        "label": "Signal TTL (seconds)",
        "help": "A queued command older than this expires rather than executing, so stale entries are not fired at the next poll.",
    },
    "dedup_window_seconds": {
        "type": int, "min": 0, "max": 86_400,
        "label": "Dedup window (seconds)",
        "help": "An identical command within this window is treated as a duplicate and not dispatched twice.",
    },
    "dispatch_timeout_seconds": {
        "type": int, "min": 5, "max": 3_600,
        "label": "Dispatch timeout (seconds)",
        "help": "A command handed to the EA but never acknowledged is marked expired after this long. It is not re-sent, because the EA dedups on the exact body.",
    },
    "paused": {
        "type": bool, "min": None, "max": None,
        "label": "Pause dispatch",
        "help": "Intake keeps recording, but nothing is handed to the EA. Use this to investigate before resuming.",
    },
    "account_poll_seconds": {
        "type": int, "min": 0, "max": 3_600,
        "label": "Account snapshot interval (seconds)",
        "help": "How often the bridge asks the EA for a read-only account and positions snapshot, injected only when the trade queue is empty. 0 disables automatic polling.",
    },
    "require_origin": {
        "type": bool, "min": None, "max": None,
        "label": "Require allowed Origin",
        "help": "Reject intake from a browser page that is not on the allow-list. Loopback binding alone does not stop a visited web page from posting a trade.",
    },
}


def mutable_view() -> Dict[str, Any]:
    """MUTABLE as JSON, so the board can render inputs from the contract.

    ``type`` holds a Python class, which cannot be serialized, and the bounds
    the board needs for its number inputs live in the same table. Rather than
    let the API layer translate it, this is the one place the shape is defined.
    """
    names = {bool: "bool", int: "int", float: "float", str: "string"}
    return {
        field: {
            "type": names[spec["type"]],
            "min": spec["min"],
            "max": spec["max"],
            "choices": list(spec["choices"]) if spec.get("choices") else None,
            "path": bool(spec.get("path")),
            "label": spec["label"],
            "help": spec["help"],
        }
        for field, spec in MUTABLE.items()
    }
