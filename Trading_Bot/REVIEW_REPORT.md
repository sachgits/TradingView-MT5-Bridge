# Bot_Trader - Signal Review & Bug Report

Program: `MQL5\Experts\Bot_Trader\Bot_Trader.mq5` (reports itself as v6.5)
Reviewed: 2026-10-12
Reviewer: MetaTrader Assistant
Scope: a queue of JSON bridge signals plus a source review of the EA that consumes them.

> Status legend: **FIXED** = patched and recompiled in this pass. **RECOMMENDED** = analysed,
> exact change described, not yet applied (needs a design decision or a multi-line edit).

---

## 0. TL;DR for the maintenance team

1. The EA **does compile** and works, but the **position-sizing path was broken** (every order was
   forced to the symbol minimum lot). This is now fixed.
2. Several execution paths reported **SUCCESS even when the broker rejected the operation**.
   The result-checking defects are now fixed for `modify`, `breakeven`, `partial_close`, `close_all`.
3. `cancel_pending` **did not return the cancelled order ticket** in its response (the maintainer
   spotted this). Now fixed.
4. The response contract is **thin**: it is missing position/deal tickets, fill price/volume and
   per-item results. Section 4 lists what the bridge should receive.
5. Some failure paths produce **no response at all** (parse/symbol failures) and spam the journal
   every poll. Section 6 item R2.
6. **Only one copy of the bot exists.** The file header claims a second identical copy
   (`MQL5\Experts\Trading_Bot\Trading_Bot.mq5`) - it is NOT present. Keep this in mind before
   "updating both".

---

## 1. What the EA expects (input contract)

The EA polls `ServerURL` (default `http://127.0.0.1:8080/signal`) and executes the latest payload.
It must be whitelisted in Tools -> Options -> Expert Advisors -> Allow Web requests.

### 1.1 Bridge envelope

```json
{
  "status": "NEW",
  "request_id": "<echoed back in the result>",
  "timestamp": "2026-10-12T04:43:01Z",
  "source": "api",
  "source_file": "",
  "normalized": { "...command fields..." },
  "signal": "{...legacy/duplicate command...}"
}
```

Rules the EA actually enforces:

- `status` must be **exactly `"NEW"`** (case-sensitive). Anything else is ignored (throttled log).
- `normalized` is the **preferred** command source; top-level is accepted; a JSON string inside
  `signal` is only a fallback and is **fragile** - it is *not* unescaped, so escaped quotes inside
  `signal` break that fallback. Do not rely on `signal`.
- Duplicate protection is a **byte-exact comparison of the whole payload** with the last processed
  one. Changing `request_id`/`timestamp` each poll **defeats dedup** and can re-execute the signal.
- On **startup only**, signals older than `StaleSignalMinutes` (default 10) are cleared. See R6.

### 1.2 Command fields (inside `normalized` or top level)

| Field | Type | Notes |
|---|---|---|
| `action` | string | required |
| `symbol` | string | optional for some actions; must be the broker's EXACT symbol name |
| `size` | number \| object | `{ "value": n, "unit": "lots|percent|risk|money", "type": "fixed" }`; `type` is ignored |
| `lot` / `value` | number | aliases for a plain lot size |
| `risk_percent` | number | used by `unit:"risk"` |
| `stop_loss` / `sl` / `sl_distance` | number \| string \| object | see 1.4 |
| `take_profit` / `tp` / `tp_distance` | number \| string \| object | see 1.4 |
| `breakeven` | object | `{ "trigger": <spec>, "lock_in": <spec> }` (trigger parsed but NOT enforced) |
| `close_percent` | number | `partial_close` only |
| `price` / `entry` | number | pending orders: absolute price, or a signed point offset |
| `price_unit` | string | `"price"` (default) or `"points"`/`"distance"` (signed offset from market) |
| `stoplimit` / `stop_limit` / `limit_price` | number | stop-limit inner limit price |
| `order_ticket` / `ticket` | number | `modify_pending` / `cancel_pending` target |
| `order_type` | string | `pending` action only; otherwise derived from `action` |
| `expiration` / `expiry` | string | `YYYY.MM.DD HH:MM[:SS]` or ISO `YYYY-MM-DDTHH:MM:SS` (server time) |

Unknown fields are ignored (`schema_version`, `type`, `source`, `action_raw` are all dead data).

### 1.3 Actions

| Action | Aliases | Needs | Notes |
|---|---|---|---|
| `buy` | long, open_buy, openlong, buy_now | symbol | market buy; SL/TP default from inputs if omitted |
| `sell` | short, open_sell, openshort, sell_now | symbol | market sell |
| `close_symbol` | close, flat, cancel | symbol | closes magic-owned positions of that symbol |
| `close_all` | flat_all, close_everything | - | closes ALL magic-owned positions (all symbols) |
| `partial_close` | close_percent, scale_out | symbol | `close_percent` (omitted = full close) |
| `modify` | modify_stop, set_sltp, update_sltp | symbol | position SL/TP |
| `breakeven` | be, move_be, break_even | symbol | SL to entry +/- `lock_in` points |
| `noop` | hold, none, wait, trend, wait_for | - | acknowledged no-op |
| `account` | status, snapshot, account_info, info | - | read-only snapshot; works offline |
| `buy_limit`/`sell_limit`/`buy_stop`/`sell_stop` | - | symbol, price | pending orders |
| `buy_stop_limit`/`sell_stop_limit` | - | symbol, price, stoplimit | pending stop-limit |
| `pending` | place_pending, pending_order, open_pending | symbol, price, order_type | generic pending |
| `modify_pending` | modify_order, update_order, update_pending | order_ticket | change price/SL/TP/expiry |
| `cancel_pending` | cancel_order, delete_order, delete_pending | order_ticket OR symbol | cancel one, or all for a symbol |

Any other action returns local guard `-13`.

### 1.4 SL/TP specification formats

- number: `"stop_loss": 200`
- string: `"sl": "200"`
- object: `{ "type":"distance", "unit":"points", "value":200 }` (`value` may be spelled `distance`)
- `unit = points` (SYMBOL_POINT), `price_distance` (absolute gap), `atr` (with `period`, `multiplier`).

`points` are **SYMBOL_POINT units**, NOT pips. For a 5-digit FX pair 1 pip = 10 points.

---

## 2. Local guard retcodes (SKIPPED before reaching the broker)

| Code | Meaning |
|---|---|
| -10 | terminal offline |
| -11 | trading disabled (terminal/account/EA) |
| -12 | `MaxPositions` reached (buy/sell only) |
| -13 | unknown action |
| -14 / -15 | market mode != FULL (buy / sell) |
| -20 | risk sizing: margin/volume problem |
| -30 | no position for partial_close |
| -31 | no position for modify |
| -32 | no position for breakeven |
| -40 | invalid pending params (type / missing price / missing stoplimit) |
| -41 | no matching pending order / not owned by this bot |
| -42 | market closed / no quote for pending |
| -43 | unparseable expiration |

`status` is derived from retcode: `SUCCESS` (0), `FAILED` (>0 broker retcode), `SKIPPED` (<0 local).

---

## 3. Confirmed bugs found in the JSON signals (source-verified)

| # | Bug | Severity | Status |
|---|---|---|---|
| B1 | `NormalizeVolume()` forced `volume_min` for every order (all sizing collapsed to 0.01) | HIGH | **FIXED** |
| B2 | `cancel_pending` response omitted the cancelled `order_ticket` | MED | **FIXED** |
| B3 | `modify` / `breakeven` / `partial_close` reported SUCCESS regardless of `trade.ResultRetcode()` | HIGH | **FIXED** |
| B4 | `breakeven` sent TP=0 to `PositionModify`, wiping existing TP | HIGH | **FIXED** |
| B5 | position `modify` overwrote an omitted `take_profit` with the default `TakeProfitPoints` (100) | HIGH | **FIXED** |
| B6 | `close_all` counted failed closes as succeeded | MED | **FIXED** |
| B7 | ATR SL/TP could silently fall back to 50/50 ("1:1") because the just-created ATR handle was read before `BarsCalculated` and from the forming bar | MED | **FIXED** |
| R1 | Negative signed `price_unit:"points"` offsets are dropped (parser + executor) | MED | RECOMMENDED |
| R2 | Parse / symbol-availability failures produce **no response** and flood the journal every poll | HIGH | RECOMMENDED |
| R3 | No pending-order price-side validation (stop/limit on wrong side of market) | MED | RECOMMENDED |
| R4 | `modify_pending` uses the CHART symbol's point/digits instead of the order's symbol | MED | RECOMMENDED |
| R5 | `noop` is suppressed (returns -10/-11) when offline or trading disabled | LOW | RECOMMENDED |
| R6 | Startup staleness compares a UTC timestamp against LOCAL time | MED | RECOMMENDED |
| R7 | Dedup is byte-exact; `request_id`/`timestamp` churn defeats it | MED | RECOMMENDED |
| R8 | `close_symbol` (`ClosePositionByMagicAndSymbol`) ignores the close result; caller always reports "CLOSED <symbol>" | MED | RECOMMENDED |
| R9 | Header/docs claim a second identical copy (`Trading_Bot.mq5`) that does not exist; version notes mix v6.4/v6.5 | LOW | RECOMMENDED |

---

## 4. Response contract - what the EA sends back, and what is MISSING

Every processed signal is acknowledged with a POST to `ProcessedURL`
(default `http://127.0.0.1:8080/signal/processed`):

```json
{ "request_id": "...", "timestamp": "...", "action": "buy", "symbol": "EURUSD",
  "size": 0.01, "status": "SUCCESS", "retcode": 0, "result": "BUY OPENED" }
```

Optional extras already present: `"order_ticket"` (placed/modified pending), `"snapshot"` (account).

### 4.1 Missing / recommended response fields

The bridge currently cannot reliably correlate an execution to the broker's objects. Recommended additions:

| Field | For | Why |
|---|---|---|
| `order_ticket` on cancel | cancel_pending | **Added (B2)** - previously absent |
| `position_ticket` | buy / sell | The opened POSITION ticket - currently NOT returned; the sender cannot track a market position |
| `deal_ticket` | buy / sell | The execution deal id |
| `filled_price`, `filled_volume` | buy / sell / partial_close | Actual fill (slippage visibility) |
| `remaining_volume` | partial_close | What is left after a partial close |
| `sl`, `tp` (applied) | modify / breakeven / modify_pending | The SL/TP actually written |
| `closed[]` / `cancelled[]` | close_all / cancel_pending(symbol) | List of affected tickets + count of failures |
| `order_symbol` | modify_pending / cancel_pending | The instrument of the target order |
| `schema_version` | all | Echo the protocol version the EA understood |
| `stage` | all | Where it stopped: `parsed`, `rejected`, `sent`, `filled` |
| `broker_retcode` vs local code | all | Distinguish broker retcode from local guard (currently |retcode| sign is the only hint) |
| `errors[]` | all | Structured reasons instead of a single `result` string |
| any response at all | parse/symbol failures | Currently NONE is sent (R2) |

Result string is currently overloaded (human text + machine meaning). Prefer `result` for humans and
add `stage`/`errors[]` for machines.

---

## 5. Fixes applied in this pass (Bot_Trader.mq5)

Compiled with MetaEditor build 6230: **0 errors, 0 warnings**.

1. **B1 `NormalizeVolume`** - was:
   ```mql5
   if(lot <= 0 || minV > 0)
      lot = minV > 0 ? minV : lot;
   lot = MathFloor(lot / step + 0.5) * step;
   ```
   The condition `|| minV > 0` was always true, so `lot` was reset to `volume_min` for every
   symbol with a minimum volume (i.e. always). Now:
   ```mql5
   if(lot <= 0)
      lot = minV > 0 ? minV : lot;
   lot = MathFloor(MathMax(lot, minV) / step + 0.5) * step;
   ```
   Sizing (lots/percent/risk/money) now survives; the minimum is still enforced.

2. **B2 cancel_pending ticket** - the success branch now also sets
   `lastTradeOrderTicket = cmd.orderTicket;` so the result body carries `order_ticket`.

3. **B3 result checks** - `ModifyPosition`, `MoveToBreakeven`, `PartialClosePosition` now capture the
   return of `trade.PositionModify` / `trade.PositionClosePartial` and set
   `lastTradeRetcode = ok ? 0 : (int)trade.ResultRetcode();` with a FAILED result string.

4. **B4 breakeven TP** - reads the current `POSITION_TP` and passes it to `PositionModify`
   instead of `0`, so the take profit is preserved.

5. **B5 modify preserves omitted legs** - `ModifyPosition` now uses the `hasStopLoss` / `hasTakeProfit`
   flags already computed by the parser and starts from the current `POSITION_SL` / `POSITION_TP`.
   Omitting `take_profit` no longer rewrites it to the 100-point default.

6. **B6 close_all honesty** - `CloseAllPositions` increments the counter only when
   `trade.PositionClose(ticket)` succeeds.

7. **B7 ATR readiness** - `GetAtrValue` now waits briefly for `BarsCalculated(handle) >= 2`
   (max 10 x 20 ms) and reads **index 1** (last closed bar) instead of index 0, so the ATR spec
   no longer silently degrades to the 50-point fallback and is deterministic within a bar.

> Note: some of these edits were applied as compact single-line replacements (the editor patch tool
> here does not support multi-line search), so a few lines hold two statements. Behaviour is correct,
> but the maintainers may want to re-flow them for style.

---

## 6. Recommended fixes (not yet applied) - with exact guidance

**R1 - negative signed point offset is dropped.**
`ParseTradeCommand`: `if(cmd.price <= 0) cmd.price = JsonGetDouble(cmdObject,"entry",0.0);` treats a
negative offset as "missing". `PlacePendingOrder`: `if(price <= 0.0)` runs **before** the
`price_unit:"points"` conversion. Fix: move the `priceInPoints` conversion above the validation, and
validate the *resolved* absolute price; only fall back to `entry` when the `price` key is truly absent.
Also note a `buy_stop_limit` with `-120` points is the wrong side for a sell - see R3.

**R2 - no acknowledgement on parse failure + journal flood.**
In `PollForSignals`, when `ParseTradeCommand` returns false nothing is sent and `lastProcessedSignal`
is not updated, so the EA re-parses (and logs) the same payload every poll (~every 750 ms).
Fix: post a `SKIPPED` result (e.g. retcode -50 "parse/symbol rejected") and set `lastProcessedSignal`.

**R3 - pending price-side validation.**
`PlacePendingOrder` never checks that `buy_stop > Ask`, `sell_stop < Bid`, `buy_limit < Ask`,
`sell_limit > Bid`; only the broker rejects (10015). Fix: add a local check returning `-40`.

**R4 - modify_pending symbol.**
Uses `SymbolInfoDouble(cmd.symbol, SYMBOL_POINT)` / `NormalizePrice(cmd.symbol, ...)` with `cmd.symbol`
= chart symbol. Use `OrderGetString(ORDER_SYMBOL)` for normalisation.

**R5 - noop placement.** Handled after the `-10`/`-11` guards; move it next to the `account` branch.

**R6 - staleness timezone.** `IsSignalStale` compares the ISO/UTC `timestamp` text against
`TimeLocal()`. Either document that `timestamp` must be server/local time, or convert UTC (`Z`).

**R7 - dedup.** Consider hashing the `normalized` content instead of the whole raw payload.

**R8 - close_symbol result.** `ClosePositionByMagicAndSymbol` returns void and ignores the close
result; its caller sets `"CLOSED " + cmd.symbol` unconditionally. Apply the same pattern as B3/B6.

**R9 - docs.** Remove the stale "two identical copies" claim or restore `Trading_Bot.mq5`.

---

## 7. Guidance for signal senders (from the reviewed queue)

Observed mistakes in the sample signals:

1. **Wrong symbol name.** `XAUUSD` is not a symbol on this terminal - the gold instrument is **`GOLD`**.
   A wrong symbol is silently rejected (no response - R2).
2. **Wrong price side.** `buy_stop` at `1.095` when EURUSD was `~1.12`; `buy_stop_limit` at `2650`
   when gold was `~4194`. A stop must be beyond the market.
3. **Placeholder tickets.** `order_ticket: 12345678` does not exist -> `-41`.
4. **Placeholder `request_id`.** `"<assigned at dispatch>"` is echoed verbatim; send a real id.
5. **`schema_version` / `type` / `source` / `action_raw`** are ignored - do not rely on them.
6. **Redundant `signal` string** duplicates `normalized`; it is only a fragile fallback.
7. **Sizing is ignored** (before B1) - after the fix, `unit:"risk"` etc. work, but note the
   `MaxOpenRiskPercent` (3%) cap can still reduce size, and the minimum-lot clamp applies.
8. **Keep the payload byte-stable** until acknowledged, or dedup will not work.

### Correct examples

```json
{ "action": "buy", "symbol": "EURUSD",
  "size": { "value": 0.01, "unit": "lots" },
  "stop_loss":   { "type": "distance", "unit": "points", "value": 200 },
  "take_profit": { "type": "distance", "unit": "points", "value": 400 } }
```
```json
{ "action": "sell", "symbol": "GOLD",
  "size": { "value": 0.05, "unit": "lots" },
  "stop_loss":   { "type": "distance", "unit": "points", "value": 500 },
  "take_profit": { "type": "distance", "unit": "points", "value": 1000 } }
```
```json
{ "action": "buy_stop_limit", "symbol": "GOLD",
  "size": { "value": 0.05, "unit": "lots" },
  "price": 4230, "stoplimit": 4228,
  "expiration": "2026-12-31T23:59:00" }
```

---

## 8. Validation performed

- `Bot_Trader.mq5` recompiled: **0 errors, 0 warnings** (MetaEditor build 6230, AVX2/FMA3).
- Source review covered: parsing, sizing, all execution paths, feedback, symbol/account guards.
- Not run: live trades or backtests. Recommend validating on a DEMO account first; one backtest is
  not proof of profitability.

## 9. Files

- Changed: `MQL5\Experts\Bot_Trader\Bot_Trader.mq5`
- Added: `MQL5\Experts\Bot_Trader\REVIEW_REPORT.md` (this file)
- Does not exist (referenced by the source header): `MQL5\Experts\Trading_Bot\Trading_Bot.mq5`
