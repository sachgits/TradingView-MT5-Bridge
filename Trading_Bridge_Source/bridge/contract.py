"""Single source of truth for the Bot_Trader wire contract.

Every capability declared here was read out of Bot_Trader.mq5 and mirrors
it exactly:

  * `ACTION_ALIASES`   -> NormalizeAction()
  * `EXECUTED_ACTIONS` -> the branches of ExecuteTradeCommand()
  * `SKIPPED_ACTIONS`  -> actions that reach the EA and return retcode -13
  * `SIZE_UNITS`       -> RiskSizePosition()
  * `DISTANCE_UNITS`   -> ResolveDistanceToPoints()
  * `LOCAL_RETCODES`   -> the negative guard codes the EA reports

The validator, the dashboard, and the docs all derive from this module.
When the EA gains a capability, declare it here first; anything the bridge
accepts but the EA cannot execute is a silently dropped trade, and
anything the EA executes but the bridge rejects is an unreachable feature.
"""

from __future__ import annotations

EA_VERSION = "6.5"
SCHEMA_VERSION = "2.1"

# --------------------------------------------------------------------------
# Actions
# --------------------------------------------------------------------------
# Canonical name -> every spelling NormalizeAction() folds into it.
ACTION_ALIASES: dict[str, tuple[str, ...]] = {
    "buy": ("buy", "long", "open_buy", "openlong", "buy_now"),
    "sell": ("sell", "short", "open_sell", "openshort", "sell_now"),
    "close_symbol": ("close", "close_symbol", "flat", "cancel"),
    "close_all": ("close_all", "flat_all", "close_everything"),
    "partial_close": ("partial_close", "close_percent", "scale_out"),
    "modify": ("modify", "modify_stop", "set_sltp", "update_sltp"),
    "breakeven": ("breakeven", "be", "move_be", "break_even"),
    "noop": ("hold", "hold_position", "none", "noop", "wait", "trend", "wait_for"),
}

# ExecuteTradeCommand() also matches "close_position" directly. It is not
# produced by NormalizeAction(), so an incoming "close_position" passes
# through unchanged and still hits that branch.
EXECUTED_ACTIONS: frozenset[str] = frozenset(ACTION_ALIASES) | {"close_position"}

ENTRY_ACTIONS: frozenset[str] = frozenset({"buy", "sell"})
CLOSE_ACTIONS: frozenset[str] = frozenset({"close_symbol", "close_position", "close_all"})
MANAGEMENT_ACTIONS: frozenset[str] = frozenset({"partial_close", "modify", "breakeven"})
NOOP_ACTIONS: frozenset[str] = frozenset({"noop"})

# Actions the previous bridge schema accepted but the EA does not implement.
# Each one reaches ExecuteTradeCommand(), falls through every branch, and is
# reported as SKIPPED with retcode -13 - a trade silently not placed. They are
# rejected at intake so the sender learns immediately instead of after the
# fact. Remove an entry here once the EA implements it.
SKIPPED_ACTIONS: dict[str, str] = {
    "buy_limit": "pending orders are not implemented by the EA",
    "sell_limit": "pending orders are not implemented by the EA",
    "buy_stop": "pending orders are not implemented by the EA",
    "sell_stop": "pending orders are not implemented by the EA",
    "close_positions": "use close_all (plural is not an EA action)",
    "modify_position": "use modify",
    "reverse": "not implemented by the EA",
    "close_pending": "not implemented by the EA",
}

_ALIAS_TO_CANONICAL: dict[str, str] = {
    alias: canonical for canonical, aliases in ACTION_ALIASES.items() for alias in aliases
}
_ALIAS_TO_CANONICAL["close_position"] = "close_position"


def normalize_action(raw: object) -> str:
    """Fold any accepted spelling to its canonical action.

    Mirrors NormalizeAction(): an unrecognised action is returned unchanged so
    the caller can report it rather than guessing.
    """
    text = str(raw or "").strip().lower()
    return _ALIAS_TO_CANONICAL.get(text, text)


def is_executed(action: str) -> bool:
    return normalize_action(action) in EXECUTED_ACTIONS


# --------------------------------------------------------------------------
# Position sizing
# --------------------------------------------------------------------------
# RiskSizePosition() branches on these. "currency" is an alias for "money".
SIZE_UNITS: frozenset[str] = frozenset({"lots", "percent", "risk", "money", "currency"})
SIZE_UNIT_ALIASES: dict[str, str] = {"currency": "money"}

# Units whose lot calculation is a documented approximation rather than exact
# risk math (see the RiskSizePosition() header comment). Surfaced on the
# signal board so an operator can see which trades used approximate sizing.
APPROXIMATE_SIZE_UNITS: frozenset[str] = frozenset({"percent", "money", "currency"})

# --------------------------------------------------------------------------
# Distances (stop loss / take profit)
# --------------------------------------------------------------------------
# ResolveDistanceToPoints() handles exactly these three. Anything else falls
# through to a hardcoded 50 points, which is why unsupported units are
# rejected rather than passed along.
DISTANCE_UNITS: frozenset[str] = frozenset({"points", "price_distance", "atr"})

# Units that look plausible, appear in older examples and TEST_MATRIX.md, but
# are NOT resolved by the EA - they silently degrade to 50 points.
UNSUPPORTED_DISTANCE_UNITS: dict[str, str] = {
    "percent": "the EA resolves only points, price_distance and atr; a percent distance silently becomes 50 points",
    "currency": "the EA resolves only points, price_distance and atr; a currency distance silently becomes 50 points",
    "absolute_price": "use price_distance for an absolute price gap",
}

DEFAULT_ATR_PERIOD = 14
DEFAULT_ATR_TIMEFRAME = "M15"
DEFAULT_ATR_MULTIPLIER = 1.0

# The EA hardcodes PERIOD_M15 when resolving ATR; "timeframe" is parsed but
# ignored. Declared so the bridge can warn instead of implying it works.
ATR_TIMEFRAME_HONOURED = False

# --------------------------------------------------------------------------
# Outcomes reported by the EA
# --------------------------------------------------------------------------
# ReportSignalResult() sets status from the sign of the retcode.
RESULT_SUCCESS = "SUCCESS"   # retcode == 0
RESULT_FAILED = "FAILED"     # retcode > 0, a broker rejection
RESULT_SKIPPED = "SKIPPED"   # retcode < 0, a local guard fired first

RESULT_STATUSES: frozenset[str] = frozenset({RESULT_SUCCESS, RESULT_FAILED, RESULT_SKIPPED})

# Negative retcodes are the EA's own guard codes, not broker codes.
LOCAL_RETCODES: dict[int, str] = {
    -10: "terminal offline",
    -11: "trading disabled in terminal, account or EA",
    -12: "MaxPositions reached",
    -13: "unknown or unimplemented action",
    -14: "market not open for buy (SYMBOL_TRADE_MODE != FULL)",
    -15: "market not open for sell (SYMBOL_TRADE_MODE != FULL)",
    -20: "risk sizing: insufficient margin or invalid volume",
    -30: "no matching open position for partial_close",
    -31: "no matching open position for modify",
    -32: "no matching open position for breakeven",
}

# Common broker retcodes, for labelling the histogram on the signal board.
BROKER_RETCODES: dict[int, str] = {
    10004: "requote",
    10006: "request rejected",
    10013: "invalid request",
    10014: "invalid volume",
    10015: "invalid price",
    10016: "invalid stops",
    10017: "trade disabled",
    10018: "market closed",
    10019: "no money",
    10020: "prices changed",
    10021: "off quotes",
    10024: "too frequent requests",
    10030: "invalid execution mode",
    10031: "no connection",
}

# MT5's own success retcodes. Bot_Trader v6.5 normalises a filled order to
# retcode 0, but a partial fill or a later EA build can surface these, and a
# success code labelled "broker rejection" would tell the operator the opposite
# of the truth about an open position.
SUCCESS_RETCODES: dict[int, str] = {
    10008: "order placed",
    10009: "request completed",
    10010: "completed partially",
}


def describe_retcode(retcode: int) -> str:
    if retcode == 0:
        return "success"
    if retcode < 0:
        return LOCAL_RETCODES.get(retcode, "unrecognised local guard code")
    if retcode in SUCCESS_RETCODES:
        return SUCCESS_RETCODES[retcode]
    return BROKER_RETCODES.get(retcode, "broker rejection")


# --------------------------------------------------------------------------
# Bridge-side signal lifecycle
# --------------------------------------------------------------------------
STATUS_QUEUED = "queued"
STATUS_DISPATCHED = "dispatched"
STATUS_EXECUTED = "executed"
STATUS_FAILED = "failed"
STATUS_SKIPPED = "skipped"
STATUS_REJECTED = "rejected"
STATUS_INVALID = "invalid"
STATUS_EXPIRED = "expired"
STATUS_CANCELLED = "cancelled"

SIGNAL_STATUSES: tuple[str, ...] = (
    STATUS_QUEUED,
    STATUS_DISPATCHED,
    STATUS_EXECUTED,
    STATUS_FAILED,
    STATUS_SKIPPED,
    STATUS_REJECTED,
    STATUS_INVALID,
    STATUS_EXPIRED,
    STATUS_CANCELLED,
)

# Terminal states: a signal in one of these is never dispatched again. This is
# what makes "do not send the same trade twice" survive a restart.
TERMINAL_STATUSES: frozenset[str] = frozenset({
    STATUS_EXECUTED,
    STATUS_FAILED,
    STATUS_SKIPPED,
    STATUS_REJECTED,
    STATUS_INVALID,
    STATUS_EXPIRED,
    STATUS_CANCELLED,
})

# Intake channels.
SOURCE_HTTP = "http"
SOURCE_FILE = "file"
SOURCE_API = "api"
SOURCE_MANUAL = "manual"
SOURCES: frozenset[str] = frozenset({SOURCE_HTTP, SOURCE_FILE, SOURCE_API, SOURCE_MANUAL})

# --------------------------------------------------------------------------
# Wire format
# --------------------------------------------------------------------------
# GET /signal envelope consumed by GetSignalFromServer()/ParseTradeCommand().
# The EA reads command fields from "normalized" first, then the top level,
# then falls back to a plain-text "signal" that contains no "{".
ENVELOPE_KEYS: tuple[str, ...] = (
    "status",
    "request_id",
    "timestamp",
    "source",
    "source_file",
    "normalized",
    "signal",
)

# Envelope status values the EA branches on. "NEW" means execute; anything
# else means idle.
ENVELOPE_NEW = "NEW"
ENVELOPE_IDLE = "PROCESSED"

# Body ReportSignalResult() POSTs back to ProcessedURL.
RESULT_FIELDS: tuple[str, ...] = (
    "request_id",
    "timestamp",
    "action",
    "symbol",
    "size",
    "status",
    "retcode",
    "result",
)

# EA input names, so the dashboard can label what the operator configured.
EA_INPUTS: tuple[str, ...] = (
    "LotSize",
    "TakeProfitPoints",
    "StopLossPoints",
    "MagicNumber",
    "MaxPositions",
    "PollIntervalMs",
    "StaleSignalMinutes",
    "HeartbeatLogSec",
    "DefaultRiskPercent",
    "MaxOpenRiskPercent",
    "MaxExposurePerSymbol",
    "MinStopDistancePoints",
    "ServerURL",
    "ProcessedURL",
    "AllowedSymbols",
    "ResultLogFile",
)

# The two folders intake works with, and the rule that separates them:
#
#   INBOX  (settings.signals_folder) - shared with other producers. The bridge
#           only ever lists, stats and reads it. No rename, no write, no delete,
#           no marking: another application owns those files and may be reading
#           them too. The bridge must work when the inbox is mounted read-only.
#   DATA   (settings.data_folder)    - owned by the bridge. Every file consumed
#           from the inbox is copied here and the copy carries the outcome
#           marking, so the audit trail survives without touching the inbox.
#
# Because the inbox can no longer be renamed to record what was handled, the
# memory of "already ingested" lives in SQLite: the file_intake table is a
# permanent register keyed on (source_path, content_hash). A file is claimed by
# inserting a row, before it is copied and before it is queued.
INBOX_READ_ONLY = True
INTAKE_SUBFOLDER = "intake"
INTAKE_INTERRUPTED_SUBFOLDER = "interrupted"

# File states. These apply to the bridge's own copy in DATA/intake, never to the
# original in the inbox. The extension is the state marker: only *.json is
# eligible for intake.
FILE_SUFFIX_PENDING = ".json"
FILE_SUFFIX_PROCESSING = ".processing"
FILE_SUFFIX_PROCESSED = ".processed"
FILE_SUFFIX_REJECTED = ".rejected"
FILE_SUFFIX_INVALID = ".invalid"
# A claim registered in SQLite but never resolved: the bridge died between
# claiming the file and recording an outcome, so whether the command reached the
# EA is unknown. Its copy is parked under this suffix for an operator to inspect
# rather than being replayed automatically.
FILE_SUFFIX_INTERRUPTED = ".interrupted"

# Ledger status -> the suffix the copied file is left with.
FILE_SUFFIX_FOR_STATUS = {
    "queued": FILE_SUFFIX_PROCESSED,
    "executed": FILE_SUFFIX_PROCESSED,
    "dispatched": FILE_SUFFIX_PROCESSED,
    "rejected": FILE_SUFFIX_REJECTED,
    "invalid": FILE_SUFFIX_INVALID,
    "duplicate": FILE_SUFFIX_PROCESSED,
}

# file_intake.outcome while the claim is open. Anything else is terminal.
INTAKE_OUTCOME_CLAIMED = "claimed"
INTAKE_OUTCOME_INTERRUPTED = "interrupted"


def public_contract() -> dict:
    """The contract as JSON, served at GET /api/v1/contract.

    The dashboard builds its "send test signal" form from this, and any signal
    producer can introspect it instead of hardcoding the vocabulary.
    """
    actions = {canonical: list(aliases) for canonical, aliases in ACTION_ALIASES.items()}
    actions["close_position"] = ["close_position"]
    return {
        "ea_version": EA_VERSION,
        "schema_version": SCHEMA_VERSION,
        "actions": actions,
        "entry_actions": sorted(ENTRY_ACTIONS),
        "close_actions": sorted(CLOSE_ACTIONS),
        "management_actions": sorted(MANAGEMENT_ACTIONS),
        "unsupported_actions": SKIPPED_ACTIONS,
        "size_units": sorted(SIZE_UNITS),
        "approximate_size_units": sorted(APPROXIMATE_SIZE_UNITS),
        "distance_units": sorted(DISTANCE_UNITS),
        "unsupported_distance_units": UNSUPPORTED_DISTANCE_UNITS,
        "atr_timeframe_honoured": ATR_TIMEFRAME_HONOURED,
        "result_statuses": sorted(RESULT_STATUSES),
        "local_retcodes": {str(k): v for k, v in sorted(LOCAL_RETCODES.items())},
        "broker_retcodes": {str(k): v for k, v in sorted(BROKER_RETCODES.items())},
        "success_retcodes": {str(k): v for k, v in sorted(SUCCESS_RETCODES.items())},
        "signal_statuses": list(SIGNAL_STATUSES),
        "envelope_keys": list(ENVELOPE_KEYS),
        "result_fields": list(RESULT_FIELDS),
        "ea_inputs": list(EA_INPUTS),
    }
