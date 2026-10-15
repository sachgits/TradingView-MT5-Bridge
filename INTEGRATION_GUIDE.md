# Signal Source Integration Guide

**Audience:** whatever *produces* commands — a TradingView strategy, a Python/Node
bot, a spreadsheet macro, a journal writer, or you with `curl`. This is the
contract for talking to the TradingBridge server: what it accepts, how to send
it, how to read the outcome, and every capability the MT5 EA (Bot_Trader
**v6.6**, command schema **v2.1**) behind it can execute.

Everything here is derived from the machine-readable contract the server itself
publishes. When in doubt, the running server is authoritative:

```bash
curl -s http://127.0.0.1:8080/api/v1/contract | python -m json.tool   # the vocabulary
curl -s http://127.0.0.1:8080/api/v1/settings                         # live settings + which are editable
curl -s http://127.0.0.1:8080/health                                  # is it up, is the EA polling
```

> ⚠️ **This places real orders on a broker account.** Build against a **demo**
> account first. The bridge refuses to guess a trade parameter, but it will
> faithfully execute a wrong one you sent deliberately.

---

## 1. The mental model

```
your producer ──┐
  (HTTP POST)   │
  (JSON file)   ├──▶  TradingBridge  ──▶  SQLite ledger  ──▶  signal board (browser)
  (extension)   │      127.0.0.1:8080        (audit)
                ┘            │
                             │  EA polls GET /signal, executes, POSTs the outcome
                             ▼
                        Bot_Trader EA (v6.6) in MetaTrader 5 ──▶ broker
```

The bridge is a **queue with an audit trail**, not a trading engine. It:

1. **Intakes** your command (three transports, below).
2. **Normalizes** it — folds aliases, canonicalizes units, fills nothing in.
3. **Validates** it — strict by default; a command that would make the EA guess
   a size/symbol/stop is *rejected*, not defaulted.
4. **Queues** it in SQLite with a `request_id` and a content hash.
5. **Dispatches** the oldest queued command to the EA on its next poll.
6. **Records** the EA's acknowledgement (status, retcode, and — new in v6.6 —
   the broker's own tickets and fill data).

Two governing rules shape everything below:

- **The bridge never invents a trade parameter.** If a field is missing where the
  EA would otherwise substitute its own input, strict validation refuses the
  command. You always know why (an `error` string), and it lands in the ledger.
- **A command is never executed twice.** Dedup is by `request_id` *and* by
  content hash. A retried POST, a re-dropped file, or a bridge restart cannot
  turn one intent into two trades.

---

## 2. Three ways to send a command

### 2.1 HTTP API (recommended for bots)

```bash
curl -X POST "http://127.0.0.1:8080/api/v1/signals" \
     -H "Content-Type: application/json" \
     -H "Origin: http://127.0.0.1:8080" \
     -d '{"command": {"action":"buy","symbol":"EURUSD","size":{"value":0.01,"unit":"lots"}}}'
```

- Wrap the command in `{"command": {...}}` **or** post the command object
  directly — both work. The wrapper exists so a command that legitimately has a
  `"command"` key is not mistaken for the envelope.
- Add `?request_id=YOUR-KEY` to supply your own idempotency key (see §6). If
  omitted, the bridge generates one and returns it.
- Response codes: **201** queued · **409** duplicate · **422** rejected/invalid.
  The body carries `ok`, `status`, `request_id`, and on failure `error`/`errors`.

**Browser-origin guard.** Intake routes require an allowed `Origin` when the
request comes from a browser. A non-browser client (curl, Python `requests`, the
EA) sends no `Origin` and is allowed. From a web page, the origin must be on the
allow-list (TradingView, or the board's own origin). Optionally set
`intake_token` and send `X-Bridge-Token: <token>` on every intake call.

### 2.2 JSON file drop (recommended for TradingView / decoupled producers)

Write a `.json` file into the **inbox** (default `C:\Trades\trade_journal`). The
bridge scans it (default every 1 s), copies it into its own data folder, and
never renames, marks, or deletes anything in the inbox — the inbox is shared and
**read-only** to the bridge.

```jsonc
// C:\Trades\trade_journal\order-0001.json
{ "request_id": "order-0001",
  "action": "buy", "symbol": "EURUSD",
  "size": { "value": 0.01, "unit": "lots" },
  "stop_loss": { "unit": "points", "value": 200 },
  "take_profit": { "unit": "points", "value": 400 } }
```

The bridge's **copy** (in `data\intake\`) is marked with the outcome:

| Suffix | Meaning |
|---|---|
| `.processed` | queued, executed, dispatched, or a duplicate of an already-handled command |
| `.rejected` | validation refused it |
| `.invalid` | not parseable as JSON / not a command |
| `.interrupted` | a claim left open by a crash — parked for a human, never auto-replayed |

Only files ending in exactly `.json` are read. Write atomically (temp file +
rename) if you can; the `file_settle_ms` window (default 300 ms) also protects
against reading a half-written file.

### 2.3 Chrome extension (TradingView Strategy Tester)

The bundled extension reads the Strategy Tester's trade list and posts to the
same API. Site settings must allow **Local Network Access** for tradingview.com.

---

## 3. The command envelope

A command is a flat JSON object. Only `action` is universally required; what
else is required depends on the action (§4). Recognized fields:

| Field | Type | Notes |
|---|---|---|
| `action` | string | The verb. Aliases fold to canonical (e.g. `long`→`buy`, `flat`→`close_symbol`, `be`→`breakeven`, `trail`→`trailing`). |
| `symbol` | string | Instrument. Required for entries/management/pending in strict mode. Omitted → the EA trades its chart symbol (legacy behaviour, warned). |
| `size` | object \| number | Volume. `{"value":0.01,"unit":"lots"}` or a bare number (→ lots). See §5. |
| `stop_loss`, `take_profit` | object \| number | Distance or absolute. A bare number is **points**. See §5. |
| `take_profits` | array | Ladder of partial take-profits: `[{"distance":{...},"close_percent":50}, ...]`. |
| `risk_percent` | number | Risk-sizing shortcut; pairs with `size.unit:"risk"`. |
| `price`, `price_unit` | number, string | Pending-order entry price. `price_unit` ∈ `price` (absolute), `points`, `distance` (signed offset). |
| `stoplimit` | number | Inner limit price for `*_stop_limit` orders (same `price_unit`). |
| `order_type` | string | For the generic `pending` action only (`buy_limit`, `sell_stop`, …). |
| `expiration` | string | Pending expiry: ISO `2026-12-31T23:59:59` or MT5 `2026.12.31 23:59`. |
| `order_ticket` | int | Target a pending order for `cancel_pending`/`modify_pending`. |
| `position_ticket` | int | **v6.6.** Target one open position for `close_position`/`modify`/`breakeven`/`partial_close` instead of every position on the symbol. Alias `position_id`. |
| `close_volume` | number | **v6.6.** Exact lots to close for `partial_close`; takes priority over `close_percent`. Alias `volume`. |
| `close_percent` | number | Percentage of the position to close (`partial_close`). |
| `trailing` | object | **v6.6.** `{"distance":50,"activation":20}` for the `trailing` action. |
| `breakeven` | object | `{"lock_in":<dist>,"trigger":<dist>}` sub-distances. |
| `deviation` | int | **v6.6.** Max slippage in points, passed to `OrderSend`. |
| `comment` | string | **v6.6.** Order comment, passed to `OrderSend`. |
| `magic` | int | Override the EA's `MagicNumber` for ownership/targeting. |
| `request_id` | string | Your idempotency key (§6). |
| `timestamp` | string | ISO-8601, informational; the ledger stamps receipt time itself. |

Unknown fields are **not dropped silently** — they are forwarded to the EA as-is
and listed as warnings on the board, so a typo surfaces rather than vanishes.

---

## 4. Capability catalog — simple to complex

Every example is a complete command body. Wrap in `{"command": …}` for the API,
or drop as a `.json` file.

### 4.1 Simplest — market entry

```json
{ "action": "buy",  "symbol": "EURUSD", "size": 0.01 }
{ "action": "sell", "symbol": "XAUUSD", "size": { "value": 0.05, "unit": "lots" } }
```

A bare-number `size` means lots. In **strict** mode a market entry must state
its own `symbol` and `size`; omit them and the command is rejected rather than
letting the EA's `LotSize` input decide your volume.

### 4.2 Entry with stops and targets

```json
{ "action": "buy", "symbol": "EURUSD", "size": 0.01,
  "stop_loss":   { "unit": "points", "value": 200 },
  "take_profit": { "unit": "points", "value": 400 } }
```

Distances accept `points`, `price_distance` (an absolute price gap), and `atr`
(`{"unit":"atr","period":14,"multiplier":1.5}`). A bare number is points.
**Note:** the EA resolves ATR on its own chart timeframe — a timeframe you
request in the ATR object is parsed but *not* honoured, and the board flags it.

### 4.3 Sizing three ways (lots, account-percent, risk)

```json
{ "action": "buy", "symbol": "EURUSD", "size": { "value": 0.02, "unit": "lots" } }
```
```json
{ "action": "buy", "symbol": "EURUSD", "size": { "value": 1.0, "unit": "percent" },
  "stop_loss": { "unit": "points", "value": 300 } }
```
```json
{ "action": "buy", "symbol": "EURUSD", "risk_percent": 1.0,
  "size": { "value": 1.0, "unit": "risk" },
  "stop_loss": { "unit": "atr", "period": 14, "multiplier": 1.5 } }
```

`percent`, `money`, and `currency` sizes are **approximate** — the EA converts
them from tick data and the result is reported as a warning, never silently
trusted. Risk-based sizing needs a `stop_loss` (the risk is meaningless without
a defined loss distance).

### 4.4 Cash-risk sizing (v6.6) — "risk exactly $150"

```json
{ "action": "buy", "symbol": "EURUSD",
  "size": { "value": 150, "unit": "risk_money" },
  "stop_loss": { "unit": "points", "value": 250 } }
```

`risk_money` (aliases `risk_cash`, `risk_amount`, `risk_usd`) is a **cash amount
in account currency**. The EA converts it to lots from the stop distance and the
symbol's tick value before `OrderSend`, so a broker that only accepts volume
still gets an exact dollar risk. **Requires a `stop_loss`** — strict mode
rejects without one, permissive warns.

### 4.5 Closing

```json
{ "action": "close_symbol",   "symbol": "EURUSD" }
{ "action": "close_position", "position_ticket": 7002 }
{ "action": "close_all" }
```

`close_position` with a `position_ticket` (v6.6) flattens **one** position; with
just a symbol it closes the symbol's position(s). `close_all` flattens
everything the bot's magic owns.

### 4.6 Partial close — by percent or exact volume

```json
{ "action": "partial_close", "symbol": "EURUSD", "close_percent": 50 }
```
```json
{ "action": "partial_close", "position_ticket": 7002, "close_volume": 0.03 }
```

`close_volume` (v6.6) closes an **exact lot size** and takes priority over
`close_percent`. Must be `> 0`.

### 4.7 Modifying stops / targets

```json
{ "action": "modify", "symbol": "EURUSD",
  "stop_loss": { "unit": "points", "value": 250 },
  "take_profit": { "unit": "points", "value": 500 } }
```
```json
{ "action": "modify", "position_ticket": 7002, "stop_loss": 120 }
```

### 4.8 Break-even

```json
{ "action": "breakeven", "symbol": "EURUSD",
  "breakeven": { "lock_in": 15, "trigger": 30 } }
```

Moves SL to lock in `lock_in` points of profit once price is `trigger` points in
favour. Sub-distances follow the same unit schema.

### 4.9 Trailing stop (v6.6)

```json
{ "action": "trailing", "symbol": "EURUSD",
  "trailing": { "distance": 50, "activation": 20 } }
```
```json
{ "action": "trailing", "position_ticket": 7002, "trailing": { "distance": 40 } }
```

Aliases `trail`, `trailing_stop`, `update_trailing`. `distance` is how far the
SL trails behind market; `activation` is the profit required before trailing
arms. Target by symbol+magic or by `position_ticket`.

### 4.10 Pending orders — the six types

```json
{ "action": "buy_stop",  "symbol": "EURUSD", "size": 0.10, "price": 1.1200, "price_unit": "price",
  "stop_loss": { "unit": "points", "value": 200 }, "take_profit": { "unit": "points", "value": 400 } }
```
```json
{ "action": "sell_limit", "symbol": "EURUSD", "size": 0.10,
  "price": -120, "price_unit": "points", "expiration": "2026-12-31T23:59:59" }
```
```json
{ "action": "buy_stop_limit", "symbol": "XAUUSD", "size": 0.01,
  "price": 4200.00, "stoplimit": 4190.00 }
```

- `price_unit: "price"` → **absolute** price (must be > 0).
- `price_unit: "points"` or `"distance"` → **signed offset** from the current
  market: positive above, negative below; a buy measures from Ask, a sell from
  Bid. A zero offset is rejected (it would not move the order).
- Stop-loss / take-profit distances on a pending are measured from the **order
  price**, not the market.
- `*_stop_limit` types additionally require the inner `stoplimit`.
- `expiration` accepts ISO or MT5 `YYYY.MM.DD HH:MM`; an unparseable one is
  refused (retcode `-43`).

The generic form takes an explicit `order_type`:

```json
{ "action": "pending", "order_type": "sell_limit", "symbol": "XAUUSD",
  "size": 0.10, "price": 4160.00 }
```

### 4.11 Cancel / modify a pending order

```json
{ "action": "cancel_pending", "order_ticket": 5001 }
{ "action": "cancel_pending", "symbol": "*" }
{ "action": "modify_pending", "order_ticket": 5001, "price": 1.1250 }
{ "action": "modify_pending", "order_ticket": 5001, "expiration": "2026.11.30 18:00" }
```

`cancel_pending`/`modify_pending` target an `order_ticket`, or a symbol plus the
bot's magic (`"*"` = all symbols). `modify_pending` needs at least one of
`price`, `stop_loss`, `take_profit`, or `expiration`. Pendings are owned by
`MagicNumber`; the EA will not touch another bot's orders (retcode `-41`).

### 4.12 Order metadata (v6.6)

Any entry/pending can carry:

```json
{ "action": "buy", "symbol": "EURUSD", "size": 0.01,
  "deviation": 20, "comment": "strategy-A v3" }
```

`deviation` is max slippage in points; `comment` is the broker order comment.
Both pass straight through to `OrderSend`.

### 4.13 Read-only account snapshot

```json
{ "action": "account" }
```

Aliases `status`, `snapshot`, `info`, `account_info`. Asks the EA for balance,
equity, margin, open positions and pending orders. It is **not a trade**: it
creates no ledger row, works even when trading is disabled or the market is
closed, and is what feeds the board's **Positions** tab. You normally do not
send this yourself — the bridge injects it automatically on an idle poll
(§7.2) — but you can force one via `POST /api/v1/account/refresh`.

### 4.14 Hold / no-op

```json
{ "action": "hold", "symbol": "EURUSD" }
```

Aliases `none`, `noop`, `wait`, `trend`, `wait_for`. Records intent without
trading — useful for a strategy that evaluates every bar but only acts on some.

### 4.15 Legacy plain text

```json
{ "signal": "BUY SL=50 TP=100 LOT=0.01" }
```

Still accepted and flagged `legacy` on the board. Symbol comes from the EA's
chart. **A body that opens like JSON but does not parse is `invalid`, never
legacy text** — the legacy parser would otherwise find the word "buy" in a
corrupt file and place a market order.

---

## 5. Units reference

**Size units:** `lots`, `percent`, `risk` (with `risk_percent`), `money`,
`currency`, and the v6.6 cash-risk family `risk_money` / `risk_cash` /
`risk_amount` / `risk_usd`. Approximate (warning-emitting): `percent`, `money`,
`currency`.

**Distance units** (for SL/TP/trailing/breakeven): `points`, `price_distance`
(absolute price gap), `atr`. **Not** resolved: `percent`, `currency`,
`absolute_price` (use `price_distance`) — the EA silently turns those into 50
points, so the bridge rejects/warns instead.

**Price units** (pending entry only): `price` (absolute), `points`, `distance`
(signed offset).

**Field aliases** the normalizer folds: `lot`/`volume`/`qty` → `size`;
`sl`/`stoploss` → `stop_loss`; `tp`/`takeprofit` → `take_profit`;
`entry` → `price`; `stop_limit` → `stoplimit`; `ticket` → `order_ticket` or
`position_ticket` (by context); `expiry` → `expiration`; `position_id` →
`position_ticket`.

---

## 6. Idempotency — how to not trade twice

This is the single most important producer responsibility.

- **Always send a stable `request_id`** derived from *your* notion of the signal
  (e.g. `"{strategy}-{bar_time}-{side}"`). `request_id` is a UNIQUE key: a
  repeat revives the existing row instead of inserting a second one.
- **Content hash dedup.** Two commands with identical normalized content inside
  the `dedup_window_seconds` are treated as a duplicate (`409`, one trade).
- **File drops.** Identical bytes at the same path are skipped by the intake
  register without re-reading. Different bytes at the same path are a new
  command. A journal-style producer that rewrites its own record in place is
  detected as a duplicate, not a second order — unless the status maps to a
  distinct action (e.g. a `closed` journal row → `close_position`, keyed
  `"{id}:close_position"`).
- **Retries mint a new id on purpose.** The board's "Retry as new" and
  `POST /api/v1/signals/<id>/retry` create a fresh `request_id`, because the EA
  dedups on the exact response body and would ignore a replay.

If your producer can crash and resend, a stable `request_id` is what makes the
resend a no-op rather than a second position.

---

## 7. Reading the outcome

### 7.1 The ledger, per signal

```bash
curl -s "http://127.0.0.1:8080/api/v1/signals/<request_id>" -H "Origin: http://127.0.0.1:8080"
```

Signal statuses: `queued` → `dispatched` → `executed` / `failed` / `skipped`,
or `rejected` (validation refused), `invalid` (not a command), `expired`
(dispatched but never acknowledged within `dispatch_timeout_seconds`, or queued
past `signal_ttl_seconds`), `cancelled` (operator removed it).

The EA's acknowledgement maps `SUCCESS`/`FAILED`/`SKIPPED` to
`executed`/`failed`/`skipped`. A late ack **supersedes** an expiry — the trade
that actually happened is the truth.

**v6.6 — broker objects (`result_extra`).** When the EA reports a fill, the
ledger now stores the broker's own identifiers so you can reconcile against the
terminal rather than trust a `"SUCCESS"` string:

```jsonc
"result_extra": {
  "order_ticket":   5001,     // placed/modified order (pendings)
  "position_ticket":7002,     // the POSITION opened/affected
  "deal_ticket":    8003,     // the execution deal
  "filled_price":   1.08421,  // actual fill (slippage visibility)
  "filled_volume":  0.01      // actual filled volume
}
```

These appear in the API row, in the `SIGNAL_RESULT` event, and in the board's
signal drawer under **Broker objects**. They are absent (not zero) when the ack
did not carry them.

### 7.2 Live account & positions

```bash
curl -s "http://127.0.0.1:8080/api/v1/positions" -H "Origin: http://127.0.0.1:8080"   # KPIs + positions + pending tables
curl -s "http://127.0.0.1:8080/api/v1/account"   -H "Origin: http://127.0.0.1:8080"    # raw snapshot state (age, in_flight)
curl -s -X POST "http://127.0.0.1:8080/api/v1/account/refresh" -H "Origin: http://127.0.0.1:8080"  # force a fresh snapshot
```

The bridge injects an `account` request to the EA **only when the trade queue is
empty**, never while paused, and only when the last snapshot is older than
`account_poll_seconds` (default 30; `0` disables). A trade always outranks a
snapshot. Snapshot fields include `balance`, `equity`, `margin`, `margin_free`,
`margin_level`, `profit`, `positions[]` (`ticket`, `symbol`, `type`, `volume`,
`open_price`, `sl`, `tp`, `profit`, `swap`, `magic`, `comment`) and
`pending_orders[]` (`ticket`, `symbol`, `type`, `state`, `volume`, `price`,
`stoplimit`, `sl`, `tp`, `expiration`).

### 7.3 Events & streaming

```bash
curl -s "http://127.0.0.1:8080/api/v1/events?since_id=0"      # poll the event log
curl -sN "http://127.0.0.1:8080/api/v1/stream"               # SSE firehose (bounded client cap)
curl -s "http://127.0.0.1:8080/api/v1/metrics"               # aggregates for the board
```

Every intake — including refusals — emits events. If SSE cannot open (too many
tabs, a buffering proxy), the board falls back to polling; the ledger is durable
either way.

### 7.4 Dry-run validation (writes nothing)

```bash
curl -s -X POST "http://127.0.0.1:8080/api/v1/validate" \
     -H "Content-Type: application/json" -H "Origin: http://127.0.0.1:8080" \
     -d '{"command": {"action":"buy_stop","symbol":"EURUSD","size":0.01,"price":120,"price_unit":"points"}}'
```

Returns `valid`, `mode`, `errors`, `warnings`, and a `summary` of what *would*
be queued — the exact checks a real submission runs, with no side effects. Use
this in CI to catch a malformed producer before it trades.

---

## 8. Retcodes

Negative values are the **EA's own guard codes** (it refused locally, no broker
round-trip). Positive values are **broker** codes. `10008/10009/10010` are MT5
success codes.

| Code | Meaning |
|---|---|
| `-10` / `-11` | terminal offline / trading disabled |
| `-12` | `MaxPositions` reached |
| `-13` | unknown or unimplemented action |
| `-14` / `-15` | market not open for buy / for sell |
| `-20` | risk sizing: insufficient margin or invalid volume |
| `-30` / `-31` / `-32` | no matching open position for `partial_close` / `modify` / `breakeven` |
| `-40` | invalid pending params (type, price or stoplimit missing) |
| `-41` | no matching pending order, or not owned by this bot's magic |
| `-42` | market closed or no quote for pending placement |
| `-43` | unparseable expiration |
| `-50` | **v6.6** unparseable command or invalid symbol, rejected locally |

Common broker codes: `10004` requote, `10006` rejected, `10013` invalid request,
`10014` invalid volume, `10015` invalid price, `10016` invalid stops, `10017`
trade disabled, `10018` market closed, `10019` no money, `10020` prices changed,
`10021` off quotes, `10024` too frequent, `10030` invalid execution mode,
`10031` no connection. The board labels each from `contract.describe_retcode`.

---

## 9. Actions the bridge refuses at intake

These reach the EA and would be silently `SKIPPED`, so the bridge rejects them
up front with the reason:

| Sent | Refused — use instead |
|---|---|
| `close_positions` | `close_all` (plural is not an EA action) |
| `modify_position` | `modify` |
| `close_pending` | `cancel_pending` |
| `reverse` | not implemented by the EA |

---

## 10. Settings a producer should know

Editable live from the board or `PATCH /api/v1/settings` (all-or-nothing: one
bad value rejects the whole batch):

| Setting | Default | Why it matters to you |
|---|---|---|
| `validation_mode` | `strict` | `strict` requires an entry to state its own `symbol` and `size`; `permissive` lets the EA's inputs supply them (testing only). |
| `dedup_window_seconds` | 60 | How long identical content is suppressed as a duplicate. |
| `signal_ttl_seconds` | — | A queued command older than this expires instead of firing at the next poll. |
| `dispatch_timeout_seconds` | — | Dispatched-but-unacknowledged becomes `expired` (never auto-resent). |
| `max_queue_depth` | — | New commands are rejected once this many are waiting. |
| `account_poll_seconds` | 30 | Idle account-snapshot cadence; `0` disables. |
| `journal_max_age_seconds` | 3600 | A trade-journal record whose trigger is older than this is refused as history. |
| `enable_file_intake` | on | Turn off to accept HTTP only. |
| `watch_interval` / `file_settle_ms` | 1 s / 300 ms | Inbox scan cadence and mid-write protection. |
| `paused` | off | Intake keeps recording; nothing is handed to the EA. |
| `require_origin` / `intake_token` | on / empty | Browser-origin allow-list and optional `X-Bridge-Token` on intake routes. |

---

## 11. A minimal Python producer

```python
import json, time, urllib.request

BASE = "http://127.0.0.1:8080"
ORIGIN = {"Content-Type": "application/json", "Origin": BASE}

def post(path, payload, method="POST"):
    req = urllib.request.Request(BASE + path, data=json.dumps(payload).encode(),
                                 headers=ORIGIN, method=method)
    with urllib.request.urlopen(req, timeout=10) as r:
        return r.status, json.load(r)

def send(command, request_id):
    # Stable request_id => a crash-and-resend is a no-op, not a second trade.
    status, body = post(f"/api/v1/signals?request_id={request_id}", {"command": command})
    if status == 201:
        return body["request_id"]
    if status == 409:
        return None  # duplicate — already handled
    raise RuntimeError(f"{status}: {body.get('error') or body.get('errors')}")

def wait_outcome(request_id, timeout=30):
    deadline = time.time() + timeout
    while time.time() < deadline:
        with urllib.request.urlopen(f"{BASE}/api/v1/signals/{request_id}", timeout=10) as r:
            row = json.load(r)
        if row["status"] in {"executed", "failed", "skipped", "rejected",
                             "invalid", "expired", "cancelled"}:
            return row
        time.sleep(0.5)
    return {"status": "timeout"}

rid = send({"action": "buy", "symbol": "EURUSD",
            "size": {"value": 150, "unit": "risk_money"},
            "stop_loss": {"unit": "points", "value": 250}},
           request_id=f"strategyA-{int(time.time())}-buy")
if rid:
    row = wait_outcome(rid)
    print(row["status"], row.get("retcode"), row.get("result_extra"))
```

---

## 12. Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| `403 origin … is not allowed` | Browser page not on the allow-list | Send from a non-browser client, use an allowed origin, or set `intake_token` and send `X-Bridge-Token`. |
| `422` with `"size" … required` | Strict mode, market entry missing size/symbol | State `symbol` and `size`, or switch to `permissive` for testing. |
| `409 duplicate` | Same content/`request_id` inside the dedup window | Expected — one trade. Use a new `request_id` for a genuinely new intent. |
| Status stuck `queued`, `ea_online:false` | EA not polling | Attach Bot_Trader to a chart, enable Algo Trading, whitelist `http://127.0.0.1:8080` in Tools → Options → Expert Advisors. |
| `expired` right after dispatch | EA never acknowledged within `dispatch_timeout_seconds` | Check the MT5 Experts journal and `ProcessedURL`; the trade may still have happened — reconcile via `result_extra`. |
| File dropped but nothing queued | Wrong folder, non-`.json` extension, adopted, or stale journal record | Check `/api/v1/watcher` (folder, `adopted_total`, `pending_files`) and `journal_max_age_seconds`. |
| Retcode `-42` on a pending | Market closed / no quote | Retry when the symbol is trading. |
| Retcode `-50` | Command unparseable or symbol invalid | Validate first with `POST /api/v1/validate`. |

---

## 13. Where to read more

- [AUDIT_AND_COMMANDS.md](AUDIT_AND_COMMANDS.md) — ledger schema, intake register, adoption, file markings, endpoints.
- [FILE_IO_GUIDE.md](FILE_IO_GUIDE.md) — folder intake in depth, trade-journal record mapping.
- [README.md](README.md) — install, the signal board, safety rules.
- [TEST_MATRIX.md](TEST_MATRIX.md) — the behaviours the suite pins down, and the manual demo procedure.
- `BROKER_CAPABILITIES.md`, `REVIEW_REPORT.md`, `MISSING_CAPABILITIES.md` (in the EA folders) — the EA-side reference and review history.
