# File I/O Mode Guide

How to drive the bridge by dropping JSON command files into a watched folder,
instead of (or alongside) posting them over HTTP.

---

## Overview

File I/O mode lets you:

- Trigger trades from any local source — a Python bot, a scheduled script, a
  hand-edited file, another program that can write to disk but not make an HTTP
  call.
- Skip TradingView Premium and the Chrome extension entirely.
- Keep the same safety guarantees as the HTTP path: every file becomes one
  ledger row with its own `request_id`, and a re-dropped file is detected as a
  duplicate rather than traded twice.

### Two folders, one rule each

| | Folder | Who owns it | What the bridge does |
|---|---|---|---|
| **Inbox** | `C:\Trades\trade_journal` | you and every other app that produces signals | lists it, stats it, reads it. **Never renames, writes, marks or deletes anything.** |
| **Data** | `C:\Trades\TradingBridge` | the bridge | keeps the SQLite ledger, a copy of every file it consumed, and the copies of anything a crash left open |

The inbox is shared, so it is treated as strictly read-only: the bridge works
even when that folder is mounted without write permission. Files stay there
forever under their original `.json` names. Each one is copied into
`data\intake\` and the **copy** carries the outcome marking (`.processed`,
`.rejected`, `.invalid`), so the audit trail survives without touching a folder
other applications are reading.

Because a handled file can no longer be renamed to mark it, the memory of what
was consumed lives in the ledger: a permanent `file_intake` register keyed on
`(source_path, content_hash)`. Inserting the row **is** the claim, and it happens
before the file is copied and before the command is queued — so a restart, a
second scanner, or the same file sitting in the inbox for a week cannot produce a
second execution.

---

## Setup

### 1. Folders

```
inbox : C:\Trades\trade_journal\  read by the bridge, written by your producers
data  : C:\Trades\TradingBridge\  written by the bridge only
        ├── bridge.sqlite3        the ledger
        ├── intake\               one copy per consumed file, marked with its outcome
        └── interrupted\          claims a crash left open, for a human to inspect
```

The data folder is set **at startup** — moving it would orphan the ledger:

```bash
python Trading_Bridge_Source/run_bridge.py --folder "D:\signals" --data "D:\bridge-data"
```

or with environment variables:

```bash
set TRADING_BRIDGE_SIGNALS_FOLDER=D:\signals
set TRADING_BRIDGE_DATA_FOLDER=D:\bridge-data
python Trading_Bridge_Source/run_bridge.py
```

Omit `--data` and it defaults to a `TradingBridge` folder beside the inbox. The
bridge refuses to start if the two are the same folder or if either is nested
inside the other, because that would break the read-only promise.

The inbox is created on first run if it does not exist, and failing to create it
is not an error — a read-only mount is a valid inbox.

The inbox itself is the one path that **can** move while the bridge runs: it is
the first field on the board's Settings tab. See [Switching the inbox](#switching-the-inbox).

### 2. Start the bridge

```bash
python Trading_Bridge_Source/run_bridge.py            # desktop panel + web board
python Trading_Bridge_Source/run_bridge.py --no-gui   # headless
```

Or run the packaged `TradingBridge.exe`.

Then open the signal board at `http://127.0.0.1:8080/`. The header pill reads
`inbox N` — how many `.json` files the bridge has not consumed yet — and its
tooltip names both folders. The **Intake** panel on the Settings tab lists scan
count, files consumed, unhandled files, files skipped as already known, adopted
files, read and intake failures, interrupted claims, the last scan time and any
watcher error; the **Fixed at startup** panel beside it shows the data folder,
the intake folder and the ledger path.

### 3. Tune the scan

These settings are on the board's **Settings** tab and apply immediately, no
restart:

| Setting | Env var | Default | Meaning |
|---|---|---|---|
| `signals_folder` | `TRADING_BRIDGE_SIGNALS_FOLDER` | `C:\Trades\trade_journal` | The inbox. Editable from the board; see below. |
| `watch_interval` | `TRADING_BRIDGE_WATCH_INTERVAL` | `1.0` s | How often the inbox is scanned. Clamped to 0.1–3600. |
| `file_settle_ms` | `TRADING_BRIDGE_FILE_SETTLE_MS` | `300` ms | How long a file must be unchanged before it is read, so a half-written file is never parsed. |
| `journal_max_age_seconds` | `TRADING_BRIDGE_JOURNAL_MAX_AGE_SECONDS` | `3600` s | A trade-journal record whose trigger stamp is older than this is refused as history, not traded. `0` disables the check. See [Trade-journal records](#trade-journal-records). |
| `enable_file_intake` | `TRADING_BRIDGE_FILE_INTAKE` | `true` | Master switch for the watcher. |

### Switching the inbox

Type a new folder into **Inbox folder (read-only)** and save. The watcher picks
it up on its next scan, the change is kept in the ledger across restarts, and an
event lands in the Events feed. The path must be absolute, must exist as a
folder or be creatable, and must not be the data folder or nested inside it; a
bad path is rejected with a reason and nothing changes.

**Everything already in the new folder is adopted, not traded.** The moment the
inbox moves, each `.json` file present there is recorded by path in the ledger's
`inbox_adoption` table and will never be consumed — whatever its content later
becomes. That is the guard against the obvious disaster: pointing the bridge at
a folder that has been collecting signals for months would otherwise queue one
trade per file. A file written *after* the switch is a normal command.

Adoption is keyed on the path, deliberately. A shared inbox is often written by
an application that updates its own records in place — a trade journal filling
in its exit price, say — and under a content-keyed rule every such update would
look like a new command. If you genuinely want a pre-existing file traded, copy
it to a new name in the inbox; the copy is a path the bridge has never seen.

Two things follow from this and are worth knowing before you type a path:

- The first scan after a *start* does not adopt. A file dropped while the bridge
  was stopped is still a command, so "start the bridge and it picks up what is
  waiting" keeps working. Only a switch adopts.
- `--folder` and `TRADING_BRIDGE_SIGNALS_FOLDER` outrank whatever the board
  saved, so a launch script always wins over last week's browser edit.

The Intake panel's **Adopted on switch** row shows how many paths are under this
promise, and how many of them are in the current inbox.

---

## File format

A file holds one JSON object. Any of these shapes are accepted.

**Legacy text command** (what the Chrome extension sends):

```json
{ "signal": "BUY SL=50 TP=100 LOT=0.01" }
```

Tokens are optional and independent — only what you actually write becomes a
field. Omitting `LOT=` leaves the size unset so the EA applies its own `LotSize`
input; the bridge never invents a size, stop or symbol. A legacy signal carries
no symbol, so the EA trades its chart symbol.

**Structured command:**

```json
{
  "symbol": "EURUSD",
  "action": "buy",
  "size": 0.1,
  "stop_loss": { "unit": "points", "value": 500 },
  "take_profit": { "unit": "points", "value": 1000 }
}
```

**Full v2.1 command** (partial take-profits, ATR stops):

```json
{
  "schema_version": "2.0",
  "symbol": "XAUUSD",
  "action": "sell",
  "size": { "value": 0.05, "unit": "lots" },
  "stop_loss": { "unit": "atr", "period": 14, "multiplier": 2 },
  "take_profits": [
    { "distance": { "unit": "price_distance", "value": 2 }, "close_percent": 50 },
    { "distance": { "unit": "price_distance", "value": 4 }, "close_percent": 50 }
  ]
}
```

**Pending order** (EA v6.5): the entry price is either absolute (`price_unit:
"price"`) or a signed offset in points from the current Ask/Bid (`price_unit:
"points"` or `"distance"` — positive above, negative below). Stop-loss and
take-profit distances are measured from the *order* price, not the market. A
stop-limit pair needs the inner `stoplimit`; an `expiration` in ISO 8601 or MT5
`yyyy.mm.dd hh:mm` form is optional.

```json
{
  "symbol": "EURUSD",
  "action": "buy_stop",
  "size": { "value": 0.01, "unit": "lots" },
  "price": 120,
  "price_unit": "points",
  "stop_loss": { "unit": "points", "value": 200 },
  "take_profit": { "unit": "points", "value": 400 },
  "expiration": "2026-12-31T23:59:59"
}
```

`cancel_pending` and `modify_pending` target an `order_ticket` (or a symbol plus
the bot's magic when no ticket is given); `modify_pending` needs at least one of
`price`, `stop_loss`, `take_profit` or `expiration`. The EA refuses a pending it
cannot place cleanly with its own guard codes `-40`..`-43` rather than guessing.

Optional envelope fields:

| Field | Notes |
|---|---|
| `request_id` | Your own idempotency key. If omitted the bridge generates one. Supplying a stable id is what makes a re-dropped file a duplicate instead of a second trade. |
| `timestamp` | ISO 8601, informational only — the ledger stamps receipt time itself. |

The complete vocabulary — every action, unit and distance form — is in
[AUDIT_AND_COMMANDS.md](AUDIT_AND_COMMANDS.md), and a machine-readable copy is
served at `GET /api/v1/contract`.

Only files ending in exactly `.json` are read from the inbox. Marked files
(`.processed` / `.rejected` / `.invalid` / `.interrupted`) live in the bridge's
own data folder and are never scanned as commands.

---

## Trade-journal records

The default inbox, `C:\Trades\trade_journal`, is written by a playbook journal,
not by a trading client. Its files are **research records** — `pair`/`direction`,
absolute `setup` prices, nested `sizing`, a `lifecycle` and a `status` — none of
which Bot_Trader understands as-is. Forwarding the whole object would also be
unsafe: the EA's JSON reader is an unscoped substring scan, so extra keys leak
into the command.

The bridge recognises that shape and extracts a lean EA command from it. Nothing
is invented: every field on the command comes from the record, and a record that
cannot be mapped safely is refused with a reason rather than guessed at.

| Journal field | Becomes | Notes |
|---|---|---|
| `pair` (or `symbol`) | `symbol` | required |
| `direction` `long`/`short` | `action` `buy`/`sell` | anything else is refused |
| `sizing.lots` | `size` (fixed lots) | omitted if absent — the EA applies its own `LotSize` |
| `setup.entry` + `setup.stop` | `stop_loss` as a `price_distance` gap | the gap from entry, so the journal's R-multiple survives; a wrong-side stop is refused |
| `setup.entry` + `setup.target` | `take_profit` as a `price_distance` gap | omitted when `target` is null |
| `sizing.pip_distance` | `stop_loss` in points | **fallback only**, used when there is no `setup.stop` and no `atr_context` |
| `management.atr_context` | `stop_loss` in ATR | used only when there is no `setup.stop` |
| `management.breakeven.trigger_r` | `breakeven.trigger` | `trigger_r` × the stop distance |
| `id` | `request_id` = `"{id}:{action}"` | the idempotency key — see below |
| `playbook`/`tag`/`book` | `strategy_id` | for the board |
| `lifecycle.triggered_at` | `timestamp` + the freshness gate | |

**The entry price is recorded, not used.** EA v6.5 ignores `price` and fills at
market; `setup.entry` is kept on the command for the board and is the origin the
stop/target gaps are measured from. Each journal command carries warnings saying
so.

**Status decides whether it trades at all.** `triggered`/`active`/`open`/`live`/
`filled`/`working` → an entry. `closed`/`stopped`/`resolved`/`completed`/
`cancelled`/`expired`/`target_hit`/`stopped_out` → a `close_position` for that
symbol. `pending`/`wait`/`watch`/`idea`/`setup`/`draft`/`candidate`/`planned`/
`armed` → refused as "not an executable trade". An unrecognised status is treated
as an entry with a warning.

**In-place updates cannot trade twice.** The journal app rewrites its own record
as the trade progresses — filling in the exit price, flipping `status` to
`closed`. Because `request_id` is the journal `id`, the updated file is the *same*
idempotency key, so it is detected as a duplicate rather than fired as a second
order. (A `status` that maps to `close_position` uses a distinct
`"{id}:close_position"` key, so flattening the position is a deliberate, separate
instruction.)

**History is refused, not traded** — `journal_max_age_seconds`. A shared inbox
accumulates old records, and pointing the bridge at a folder of them must not
queue one market order per file. A journal record whose `lifecycle.triggered_at`
is older than `journal_max_age_seconds` (default `3600`) is rejected with
`journal trigger ... is ...s old, older than journal_max_age_seconds`. The refusal
is visible on the board and in the ledger; nothing is queued. Set it to `0` to
disable the check. Native (non-journal) commands are never subject to this gate.
This is the second layer over [adoption](#switching-the-inbox): adoption guards a
folder the inbox *switched* to, the age gate guards a record that is simply old.

A worked example — `20260922T195825Z_FLO_GBPCHF_long.json` becomes:

```json
{
  "action": "buy",
  "symbol": "GBPCHF",
  "size": { "type": "fixed", "unit": "lots", "value": 0.02 },
  "stop_loss": { "type": "distance", "unit": "price_distance", "value": 0.00221 },
  "take_profit": { "type": "distance", "unit": "price_distance", "value": 0.00442 },
  "request_id": "20260922T195825Z_FLO_GBPCHF_long:buy",
  "strategy_id": "Flossbach Liquidation Sweep"
}
```

The original record is never rewritten; it is kept whole in the ledger's
`raw_payload` for audit.

---

## Lifecycle

```
you write C:\Trades\trade_journal\my_command.json     (inbox: read-only to us)
        |
watcher waits file_settle_ms for the write to finish
        |
stat check: same size+mtime as a known file? -> skip, cost one stat
        |
read bytes, sha256
        |
INSERT INTO file_intake (path, hash) -> the claim.  Already there? -> skip.
        |
copy bytes to data\intake\my_command.processing      (our folder, our file)
        |
parse + validate -> ledger row, status queued
        |
close the claim: outcome + copy name, rename the copy
        |                -> my_command.processed | .rejected | .invalid
EA polls GET /signal and receives the envelope
        |
EA executes, POSTs /signal/processed with retcode + result text
        |
ledger row -> executed | failed | skipped, events recorded
```

The `INSERT` **is** the claim. `(source_path, content_hash)` is `UNIQUE`, so two
scanners racing, or a watcher racing a restart, cannot both consume one file —
the loser's insert fails and it moves on. Nothing is written to the inbox at any
point, and the bridge refuses at the code level to write outside its own folders.

If the process dies after claiming but before recording an outcome, the claim
stays open in the ledger. On the next start it is closed as `interrupted`, its
copy is moved to `data\interrupted\`, and an event tells you to check the board —
it is **not replayed**, because it may already have reached the EA before the
crash. The inbox file is untouched: it still says `.json` and simply will not be
consumed again. Requeue it from the board once you have confirmed on the broker
side that no position was opened.

Rewriting a file the bridge has already consumed is safe in both directions:

- **identical bytes** → the `(path, hash)` pair is already registered, so it is
  skipped without being queued again.
- **different bytes** → a new hash, so it is treated as a new command and
  consumed normally.

Latency is roughly `watch_interval / 2 + file_settle_ms`, plus the EA's poll
interval (750 ms by default): about 1.5 s end to end with the stock settings. A
scan of an inbox whose files were all handled already costs one `stat` per file.

---

## Outcome markings

These suffixes appear on the bridge's **copies** in `data\intake\`, never on your
originals in the inbox.

| Suffix | Meaning |
|---|---|
| `.json` | in the inbox: not consumed, or consumed and left exactly as it was |
| `.processing` | copy made, outcome not yet recorded |
| `.processed` | accepted and queued (or refused as a known duplicate) |
| `.rejected` | parsed fine but failed validation — the reason is in the ledger and on the board |
| `.invalid` | not JSON, or truncated |
| `.interrupted` | in `data\interrupted\`: a claim a crash left open; needs a human decision |

A name collision (the same filename consumed twice) never overwrites: the second
copy gets `.2`, `.3`, and so on.

The durable record is the `file_intake` register, not the filenames:

| Column | Meaning |
|---|---|
| `source_path` | full path in the inbox |
| `content_hash` | SHA-256 of the bytes as read |
| `size`, `mtime_ns` | the stat signature used to skip an unchanged file cheaply |
| `request_id` | the signal this file produced |
| `outcome` | `claimed` (open), `queued`, `executed`, `rejected`, `invalid`, `duplicate`, `interrupted` |
| `copy_name` | the marked copy in `data\intake\` |
| `claimed_at`, `resolved_at` | when the claim opened and closed |

To send the same command again deliberately, give it a new `request_id`, write it
under a new filename with different content, or use the board's **Retry as new**
button.

The inbox accumulates history and that is fine — pruning it is your other
applications' business, not the bridge's. If you do delete a file and later write
a new one at the same path with the same bytes, it is still recognised as
already consumed; that is intentional, since the alternative is a second
execution of a trade that may already have happened.

---

## Examples

### Python

```python
import json, time
from pathlib import Path

FOLDER = Path(r"C:\Trades\trade_journal")

def send(command: dict) -> Path:
    path = FOLDER / f"cmd_{int(time.time() * 1000)}.json"
    # Write to a temp name, then rename. A reader that opens the file mid-write
    # sees truncated JSON and the bridge parks it as .invalid.
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(command), encoding="utf-8")
    tmp.replace(path)
    return path

send({"request_id": "bot-0001", "symbol": "EURUSD", "action": "buy", "size": 0.1})
```

### PowerShell

```powershell
@{ request_id = "ps-0001"; signal = "SELL SL=75 TP=150 LOT=0.02" } |
  ConvertTo-Json |
  Set-Content "C:\Trades\trade_journal\ps_signal.json" -Encoding UTF8
```

### Manual test

Create `C:\Trades\trade_journal\test.json`:

```json
{ "signal": "BUY SL=50 TP=100 LOT=0.01" }
```

It appears on the board within one scan interval, `test.json` stays in the inbox
exactly as you wrote it, and `data\intake\test.processed` appears. To try a
command without touching a live account, use the board's **Send** tab with the
same body, or `POST /api/v1/validate`, which returns the normalized command and
every validation error without queueing anything.

---

## Hybrid mode

File and HTTP intake feed the same FIFO queue and the same ledger, so they
interleave safely and both are visible on the board (the `source` column reads
`file` or `api`).

```bash
curl -X POST http://127.0.0.1:8080/api/v1/signals ^
  -H "Content-Type: application/json" ^
  -d "{\"symbol\": \"EURUSD\", \"action\": \"buy\", \"size\": 0.1}"
```

`POST /signal` remains as a compatibility alias for the legacy extension.

If `TRADING_BRIDGE_INTAKE_TOKEN` is set, HTTP intake must send it. **File intake
is not token-gated** — anything that can write to the inbox can request a trade.
That folder's ACLs are your authentication: keep it off network shares and
writable only by producers you trust. Note that making the inbox read-only for
the bridge's own user does not reduce that risk, it only stops the bridge writing
there; a producer that can write to it can still ask for a trade.

---

## Troubleshooting

**Files sit there as `.json` and nothing happens.** Check the Intake panel on the
Settings tab: is `enable_file_intake` on, is the last-scan time advancing, is
there a `last_error`? Press **Scan folder** on the Board tab to force an
immediate pass. Then confirm the file ends in exactly `.json` and that the inbox
you are writing to is the one the bridge reports — a `--folder` argument wins over
both the default and `TRADING_BRIDGE_SIGNALS_FOLDER`, and a folder you switched to
from the board is kept in the ledger across restarts.

**I dropped a file in and nothing happened, right after switching folders.** The
file was already there when the inbox moved, so it was adopted rather than
traded — see [Switching the inbox](#switching-the-inbox). The **Adopted on switch**
row on the Intake panel counts them. Copy the file to a new name in the inbox to
trade it deliberately.

**A file was consumed but never traded, and re-dropping it does nothing.** That is
the register working: identical bytes at the same path are already recorded. Check
the signal's status on the board. To send it again use a new `request_id`, or
**Retry as new**.

**A copy is `.invalid`.** The body did not parse as JSON. A file that opens
with `{` but is truncated is treated as malformed, not as legacy text: the legacy
parser would otherwise find the word `buy` inside the fragment and turn it into a
market order. Check for a missing brace, a stray comma, or a writer that flushes
in pieces — write to a temp name and rename, as in the example above.

**A copy is `.rejected`.** It parsed, but the command is not tradeable: an
unknown action, a size of zero, a stop on the wrong side of entry. The exact
errors are on that row in the board and in `GET /api/v1/signals/<request_id>`.

**`Read failures` is climbing.** The bridge could not open an inbox file even
after the settle window — antivirus holding it, or a network share that dropped.
The file is left alone and retried on the next scan, so this is usually transient;
if it persists, check the folder's sharing and permissions.

**Nothing trades and the row stays `queued`.** The EA has not collected it. Look
at the EA heartbeat on the board: `last_seen` goes stale when MT5 is closed, when
the EA is not attached to a chart, or when "Allow WebRequest for listed URL" does
not include `http://127.0.0.1:8080`. A queued signal expires after
`signal_ttl_seconds` (120 s default) and moves to `expired`.

**Something appeared in `data\interrupted\`.** A previous run stopped between
claiming a file and recording an outcome. Deliberate; see the lifecycle section.
Confirm on the broker side whether the position exists, then leave it alone or
requeue the signal from the board.

**The bridge refuses to start with "the data folder cannot be the watch folder".**
The two must be separate and neither may contain the other, or the bridge would be
writing into the shared inbox. Point `--data` somewhere else.

**You want the inbox pruned.** The bridge will never do that — it does not delete
or rename anything there. Archiving old signals is the job of whichever
application owns the folder. The bridge's own copies in `data\intake\` are safe to
delete at any time; the durable record is the ledger.

---

## API reference

Legacy compatibility endpoints, used by Bot_Trader:

| Method | Path | Purpose |
|---|---|---|
| GET | `/signal` | Current envelope for the EA (`status`: `NEW` / `PROCESSED` / `NONE`) |
| POST | `/signal/processed` | EA reports retcode and result text |
| POST | `/signal` | Legacy intake |
| POST | `/signal/clear` | Clear the queue |
| GET | `/health` | Liveness |

Board and integration endpoints:

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/v1/state` | KPIs, queue depth, EA heartbeat, watcher status |
| GET | `/api/v1/signals` | Paged, filterable signal list |
| POST | `/api/v1/signals` | Structured intake |
| GET | `/api/v1/signals/<id>` | One signal with its full event timeline |
| POST | `/api/v1/signals/<id>/cancel` | Pull a queued signal before the EA sees it |
| POST | `/api/v1/signals/<id>/retry` | Resubmit under a fresh `request_id` |
| POST | `/api/v1/signals/<id>/requeue` | Put an expired or cancelled signal back |
| POST | `/api/v1/validate` | Dry run: normalized command plus errors, nothing queued |
| GET | `/api/v1/events` | Poll the event ledger (`since_id` cursor) |
| GET | `/api/v1/stream` | SSE version of the same events |
| GET | `/api/v1/metrics` | Status totals, retcode histogram, latency percentiles |
| GET / PATCH | `/api/v1/settings` | Read or change runtime settings |
| GET | `/api/v1/contract` | The wire protocol, as data |
| GET | `/api/v1/watcher` | Inbox counts, data folder, scan state |
| POST | `/api/v1/rescan` | Scan the inbox immediately |

---

## A note on filesystem-event watchers

Polling is deliberate. A `watchdog`-style event watcher would shave a few hundred
milliseconds off intake, but it fires for a file the moment it is created — which
on Windows is frequently before the writer has flushed its content — and it
delivers events from an arbitrary thread with no ordering guarantee. The settle
window plus the ledger claim is what makes intake idempotent, and it costs about
one second. Do not replace it with a raw `on_created` handler.

It is also worth being explicit about what polling buys on a read-only inbox: the
bridge has no way to mark a file as handled in the folder itself, so "have I seen
this?" has to be answered from its own state anyway. Once that is true, an event
subscription would only save the directory listing — it would not remove the need
for the register, and it would reintroduce the mid-write race the settle window
exists to avoid.

---

## See also

- [README.md](README.md) — architecture, install, safety rules
- [AUDIT_AND_COMMANDS.md](AUDIT_AND_COMMANDS.md) — command vocabulary, ledger schema, markings
- [INSTALL_AND_TEST_GUIDE.md](INSTALL_AND_TEST_GUIDE.md) — getting MT5 talking to the bridge
- [TEST_MATRIX.md](TEST_MATRIX.md) — what the suite covers, plus a manual demo script
- [Bot_Trader.mq5](Bot_Trader/Bot_Trader.mq5) — the Expert Advisor
- [run_bridge.py](Trading_Bridge_Source/run_bridge.py) — entry point
- [bridge/watcher.py](Trading_Bridge_Source/bridge/watcher.py) — read-only inbox intake
- [bridge/store.py](Trading_Bridge_Source/bridge/store.py) — the `file_intake` register
