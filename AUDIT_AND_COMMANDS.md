# Command schema, ledger and audit behaviour

Schema version **2.1** · EA **Bot_Trader v6.5** · the live reference is `bridge/contract.py`, and the board's **EA contract** tab renders the same table from the running process.

## Command shapes

Structured command:

```json
{
  "action": "buy",
  "symbol": "EURUSD",
  "size": { "value": 0.01, "unit": "lots" },
  "stop_loss": { "unit": "points", "value": 200 },
  "take_profit": { "unit": "points", "value": 400 }
}
```

Legacy plain text and the extension's envelope are still accepted and are flagged `legacy` in the ledger:

```json
{ "signal": "BUY SL=50 TP=100 LOT=0.01" }
```

### Fields

| Field | Meaning |
|---|---|
| `action` | Market: `buy`, `sell`. Close: `close`/`close_position`, `close_all`, `partial_close`. Manage: `modify`, `breakeven`, `hold`. Pending (v6.5): `buy_limit`, `sell_limit`, `buy_stop`, `sell_stop`, `buy_stop_limit`, `sell_stop_limit`, `pending` (+`order_type`), `cancel_pending`, `modify_pending`. Read-only: `account`. Anything else is refused. |
| `symbol` | Broker symbol. Omitted only by legacy commands, where the EA substitutes its chart symbol (warned). `cancel_pending`/`modify_pending` may target an `order_ticket` instead. |
| `size` | `{"value", "unit"}` with unit `lots`, `percent`, `risk`, `money`/`currency`, or a bare number of lots. Aliases `lot`, `volume`, `qty`, `amount` are canonicalized and removed. |
| `risk_percent` | Risk-based sizing; validated 0-100. |
| `stop_loss`, `take_profit` | Distance object: `points`, `price_distance`, `atr` (`period`, `multiplier`; the EA hardcodes M15 for ATR and says so in a warning). A bare number means points. Aliases `sl`, `stoploss`, `tp`, `takeprofit`. On a pending order these are measured from the order price, not the market (warned). |
| `close_percent` | For `partial_close`, 0-100. Omitted means close the whole position (warned). |
| `breakeven` | `{"trigger", "lock_in"}` distances. The EA moves the stop immediately; `trigger` is parsed but not enforced (warned). |
| `trailing` | Parsed but **not executed** by EA v6.5 (warned). |
| `take_profits` | Parsed but **not executed** by EA v6.5. A malformed ladder warns; it never vetoes a valid entry. |
| `price`, `price_unit`, `stoplimit`, `order_ticket`, `expiration`, `order_type` | The pending-order fields (v6.5); see below. On a market `buy`/`sell` a `price` is carried for the record but ignored (warned). |
| `magic` | Forwarded, but the EA uses its own `MagicNumber` input for position ownership (warned). |
| `request_id` | Optional idempotency key. Reusing one is always a duplicate; if the existing row never reached the broker the row is revived instead of refused. |

Unknown fields are forwarded to the EA as-is and listed as warnings - the bridge does not silently drop what it does not understand.

## Incomplete commands are refused, not guessed

The bridge **never invents a trade parameter**. In strict mode a structured entry without a size is rejected, because the alternative is the EA's `LotSize` input deciding the volume of a live order. Permissive mode (Settings tab) downgrades the same findings to warnings for testing.

A body that opens like JSON but does not parse (`{"action": "buy", "sym`) is **invalid**, never legacy text: the legacy parser would find the word `buy` and place a market order from a corrupt file.

## Pending orders and the account snapshot (EA v6.5)

Bot_Trader v6.5 grew a pending-order engine and a read-only account query. Both are real commands the bridge validates and forwards; neither changes the "never invent a parameter" rule.

**Pending entries** (`buy_limit`, `sell_limit`, `buy_stop`, `sell_stop`, and the two stop-limit types, or `action: "pending"` with an explicit `order_type`):

| Field | Meaning |
|---|---|
| `price` | Required. An absolute trigger price, or a signed offset from market when `price_unit` is `points`/`distance` (+ above, − below). `0` with a distance unit is refused: it would rest exactly at the market. Alias `entry`. |
| `price_unit` | `price` (default), `points`, or `distance`. |
| `stoplimit` | The inner limit price; required for `buy_stop_limit`/`sell_stop_limit`. Aliases `stop_limit`, `limit_price`. |
| `expiration` | ISO (`2026-12-31T23:59:00`) or MT5 (`2026.12.31 23:59`); validated the way the EA parses it, so an unparseable expiry is refused at intake instead of being skipped (-43). Absent means GTC. Alias `expiry`. |
| `stop_loss`, `take_profit` | Distances measured **from the order price**, not the market (warned). |
| `size`, `symbol` | As for a market entry; a symbol is required in strict mode. |

**Pending management**: `cancel_pending` and `modify_pending` need an `order_ticket` (alias `ticket`) or a `symbol`; `modify_pending` additionally needs at least one of `price`, `stop_loss`, `take_profit`, `expiration` — a modify that changes nothing is refused rather than forwarded.

**Account snapshot** (`action: "account"`): a pure state query. The EA builds a snapshot of the account, its open positions and its pending orders and returns it in the acknowledgement's `snapshot` field; it never submits an order and works even when trading is disabled or the market is closed. The bridge asks for one automatically every `account_poll_seconds` (default 30, `0` disables) but **only when the trade queue is empty**, so it can never delay an order, and never while dispatch is paused. Each request carries a fresh `request_id` and timestamp because the EA dedups on the whole response body. The snapshot is stored verbatim in the runtime table — not in the signal ledger, because it is not a trade — and served read-only at `GET /api/v1/account` and `GET /api/v1/positions`, and inside `GET /api/v1/state` for the board's **Positions** tab. `POST /api/v1/account/refresh` asks for one immediately (guarded like intake). The board shows the last snapshot the EA sent and how old it is; it never invents numbers.

## The ledger

Every intake produces a row in SQLite (`signals`) plus events (`events`), including refusals:

| Status | Meaning |
|---|---|
| `queued` | Validated, waiting for the EA's next poll |
| `dispatched` | Handed to the EA, acknowledgement pending |
| `executed` / `failed` / `skipped` | The EA's acknowledgement, mapped from `SUCCESS` / `FAILED` / `SKIPPED` |
| `rejected` | Validation refused it (reason in `error`) |
| `invalid` | Not a command at all (bad JSON, empty body) |
| `expired` | Dispatched but never acknowledged within the dispatch timeout, or queued longer than the signal TTL |
| `cancelled` | Removed from the queue by the operator |

Stored per row: `request_id`, content hash, source (`api` / `file` / `extension` / `manual`), source filename, raw payload, normalized command, action, symbol, size, warnings, error, retcode, result text, and the timestamps `created_at` / `dispatched_at` / `finished_at` with derived `queue_ms` and `exec_ms`.

Retcodes: negative values are the EA's own guard codes, positive values are broker codes, and `10008/10009/10010` are MT5 success codes. The board's histogram labels each one from `contract.describe_retcode`.

The negative guards the EA raises itself, so a failure never looks like a broker rejection:

| Code | Meaning |
|---|---|
| `-10` / `-11` | terminal offline / trading disabled |
| `-12` | `MaxPositions` reached |
| `-13` | unknown or unimplemented action |
| `-14` / `-15` | market not open for buy / for sell |
| `-20` | risk sizing: insufficient margin or invalid volume |
| `-30` / `-31` / `-32` | no matching open position for `partial_close` / `modify` / `breakeven` |
| `-40` | invalid pending params (order type, price or stoplimit missing) |
| `-41` | no matching pending order, or not owned by this bot's magic |
| `-42` | market closed or no quote for pending placement |
| `-43` | unparseable expiration |

## The intake register

The inbox is read-only, so a consumed file cannot be renamed to mark it. `file_intake` is the memory instead, and it is permanent rather than windowed:

| Column | Meaning |
|---|---|
| `source_path` | full path of the file in the inbox |
| `content_hash` | SHA-256 of the bytes as read |
| `size`, `mtime_ns` | stat signature, so an unchanged file is skipped without being read again |
| `request_id` | the signal this file produced |
| `outcome` | `claimed` while open, then `queued` / `rejected` / `invalid` / `duplicate` / `interrupted` |
| `copy_name` | the marked copy in the data folder's `intake/` |
| `claimed_at`, `resolved_at` | when the claim opened and closed |

`UNIQUE(source_path, content_hash)` is what makes intake safe: inserting the row **is** the claim, and it happens before the copy and before the queue. Two scanners racing, or a watcher racing a restart, cannot both consume one file. A row still open at startup means the bridge died mid-intake - it is closed as `interrupted`, its copy is set aside, and it is never replayed automatically.

Identical bytes rewritten at the same path are skipped. Different bytes at the same path are a new command.

## Adoption

The inbox is one of the settings on the board, so it can move while the bridge runs. When it moves, every `.json` file already in the new folder is recorded by path in `inbox_adoption` and is **never consumed**, whatever its content later becomes:

| Column | Meaning |
|---|---|
| `source_path` | full path, the primary key |
| `folder` | the inbox it was adopted into |
| `size`, `mtime_ns` | stat signature at adoption, for the audit trail |
| `adopted_at` | when the switch happened |

Keyed on path rather than content on purpose: a shared inbox is often written by an application that updates its own records in place, and a content-keyed rule would turn every such update into a new command. To trade an adopted file deliberately, copy it to a new name in the inbox. The first scan after a *start* does not adopt — only a switch does — so a file dropped while the bridge was stopped is still a command.

## Trade-journal records

The default inbox is written by a playbook journal whose files are research records (`pair`/`direction`, absolute `setup` prices, nested `sizing`, `lifecycle`, `status`), not EA commands. The bridge recognises that shape and extracts a lean command from it — never inventing a parameter, refusing anything it cannot map safely. Full field mapping is in [FILE_IO_GUIDE.md → Trade-journal records](FILE_IO_GUIDE.md#trade-journal-records).

Two safety properties matter for audit:

- **Idempotency across in-place edits.** `request_id` is the journal `id`, so when the journal rewrites its own record (exit price filled in, `status` flipped) the file is the same key and is detected as a duplicate, not a second order. A `status` that maps to `close_position` uses a distinct `"{id}:close_position"` key, so flattening is a deliberate separate instruction.
- **Freshness gate — `journal_max_age_seconds`** (default `3600`, `0` disables). A journal record whose `lifecycle.triggered_at` is older than the limit is rejected — status `rejected`, error `journal trigger ... is ...s old, older than journal_max_age_seconds` — and nothing is queued. This is the second layer over adoption: adoption guards a folder the inbox *switched* to, the age gate guards a record that is simply old. Native (non-journal) commands are never subject to it.

A journal command carries `_meta.is_journal = true` and `_meta.journal` (the record's `id`/`book`/`tag`/`playbook`/`tier`/`status`) for the board; the original record is kept whole in `raw_payload`.

## Two folders

| | Path | Access |
|---|---|---|
| Inbox | `C:\Trades\trade_journal` (default) | **read-only** - listed, stat'd, read. Never renamed, written or deleted. Editable from the board |
| Data | `C:\Trades\TradingBridge` (default) | bridge-owned: `bridge.sqlite3`, `intake/`, `interrupted/`. Fixed at startup |

The bridge refuses to start if the two are the same folder or if either is nested inside the other, and `FolderWatcher` raises rather than write anywhere outside its own folders. The same check runs when the inbox is changed from the board, so a bad path is rejected there too.

## File intake markings

Markings are applied to the bridge's **copy** in `data\intake\`, never to the original:

| Suffix | Meaning |
|---|---|
| `.processing` | copy made, outcome not yet recorded |
| `.processed` | Queued, or a duplicate of an already-handled command |
| `.rejected` | Validation refused the command |
| `.invalid` | Not parseable as JSON / not a command |
| `.interrupted` | In `data\interrupted\`: a claim left open by a crash - parked for a human, never replayed automatically |

Name collisions get a numeric suffix (`trade.processed.2`). The ledger, not the folder, is the audit history; the copies are safe to delete at any time.

## Endpoints

EA-facing: `GET /signal`, `POST /signal/processed`, `POST /signal` (legacy), `POST /signal/clear`, `GET /health`.

Operator: `GET|POST /api/v1/signals`, `GET /api/v1/signals/<id>`, `POST /api/v1/signals/<id>/cancel|retry|requeue`, `POST /api/v1/validate`, `GET /api/v1/events`, `GET /api/v1/metrics`, `GET|PATCH /api/v1/settings`, `GET /api/v1/contract`, `GET /api/v1/watcher`, `GET /api/v1/state`, `POST /api/v1/rescan`, `GET /api/v1/account`, `GET /api/v1/positions`, `POST /api/v1/account/refresh`, `GET /api/v1/stream`.

The three account routes feed the board's Positions tab: `/api/v1/account` returns the raw snapshot state (`snapshot`, `at`, `age_seconds`, `poll_seconds`, `in_flight`), `/api/v1/positions` lifts the balance/equity/margin KPIs plus the `positions` and `pending_orders` arrays out of it, and `/api/v1/account/refresh` sets the force flag so the next idle poll re-requests a snapshot even if the current one is still fresh. The refresh is intake-guarded like every other write, so a cross-origin page cannot spam it. None of them create a ledger row — a snapshot is read-only telemetry, not a trade.

The envelope the EA receives keeps `status`, `request_id`, `timestamp`, `source`, `source_file` **before** the nested `normalized` command and the escaped `signal` string last, because Bot_Trader's JSON reader matches the first textual occurrence of a key.
