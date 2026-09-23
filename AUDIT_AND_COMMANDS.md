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
| `action` | `buy`, `sell`, `close_position`, `close_all`, `partial_close`, `modify`, `breakeven`, `hold`. Anything else is refused. |
| `symbol` | Broker symbol. Omitted only by legacy commands, where the EA substitutes its chart symbol (warned). |
| `size` | `{"value", "unit"}` with unit `lots`, `percent`, or a bare number of lots. Aliases `lot`, `volume`, `qty`, `amount` are canonicalized and removed. |
| `risk_percent` | Risk-based sizing; validated 0-100. |
| `stop_loss`, `take_profit` | Distance object: `points`, `pips`, `price_distance`, `price`, `atr` (`period`, `multiplier`; the EA uses its chart timeframe for ATR and says so in a warning). A bare number means points. Aliases `sl`, `stoploss`, `tp`, `takeprofit`. |
| `close_percent` | For `partial_close`, 0-100. Omitted means close the whole position (warned). |
| `breakeven` | `{"trigger", "lock_in"}` distances. The EA moves the stop immediately; `trigger` is parsed but not enforced (warned). |
| `trailing` | Parsed but **not executed** by EA v6.5 (warned). |
| `take_profits` | Parsed but **not executed** by EA v6.5. A malformed ladder warns; it never vetoes a valid entry. |
| `magic`, `price` | Forwarded/ignored by the EA; both produce warnings explaining what the EA actually does. |
| `request_id` | Optional idempotency key. Reusing one is always a duplicate; if the existing row never reached the broker the row is revived instead of refused. |

Unknown fields are forwarded to the EA as-is and listed as warnings - the bridge does not silently drop what it does not understand.

## Incomplete commands are refused, not guessed

The bridge **never invents a trade parameter**. In strict mode a structured entry without a size is rejected, because the alternative is the EA's `LotSize` input deciding the volume of a live order. Permissive mode (Settings tab) downgrades the same findings to warnings for testing.

A body that opens like JSON but does not parse (`{"action": "buy", "sym`) is **invalid**, never legacy text: the legacy parser would find the word `buy` and place a market order from a corrupt file.

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

## Two folders

| | Path | Access |
|---|---|---|
| Inbox | `C:\Trades\signals` (default) | **read-only** - listed, stat'd, read. Never renamed, written or deleted. Editable from the board |
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

Operator: `GET|POST /api/v1/signals`, `GET /api/v1/signals/<id>`, `POST /api/v1/signals/<id>/cancel|retry|requeue`, `POST /api/v1/validate`, `GET /api/v1/events`, `GET /api/v1/metrics`, `GET|PATCH /api/v1/settings`, `GET /api/v1/contract`, `GET /api/v1/watcher`, `GET /api/v1/state`, `POST /api/v1/rescan`, `GET /api/v1/stream`.

The envelope the EA receives keeps `status`, `request_id`, `timestamp`, `source`, `source_file` **before** the nested `normalized` command and the escaped `signal` string last, because Bot_Trader's JSON reader matches the first textual occurrence of a key.
