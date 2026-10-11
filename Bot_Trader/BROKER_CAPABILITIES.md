# Bot_Trader ↔ Broker Capabilities Reference

Captured live from **FxPro MT5 Demo** (account **592065306**, USD, margin mode **hedging**), terminal build 6230, Bot_Trader **v6.5**.
This document tells you (server-side team) exactly which broker/account capabilities exist, how to ask for them through Bot_Trader, and how to interpret the answers. Values marked *live* were read on 2026-10-07 and can change per symbol, session or broker policy — always re-check at runtime instead of hard-coding them.

---

## 1. How the bridge works (30-second recap)

```
signal sender (TradingView / your server)
   │  GET  http://127.0.0.1:8080/signal        (returns latest envelope)
   ▼
Bot_Trader (MT5 EA, universal symbol mode)
   │  parses "action"/"symbol"/"size"/... from "normalized" or top level
   │  executes against the MT5 account
   ▼
   │  POST http://127.0.0.1:8080/signal/processed
   │  body: {"request_id","timestamp","action","symbol","size",
   │         "status","retcode","result"[, "order_ticket"][, "snapshot"]}
   ▼
signal sender receives the ack + any extra data
```

- The EA polls every `PollIntervalMs` (default 750 ms).
- Only a payload with `"status":"NEW"` that differs from the last processed signal is executed (built-in dedup).
- Signals older than `StaleSignalMinutes` (default 10) are cleared on startup.
- All trading is filtered by `MagicNumber` (positions AND pending orders).
- The account is **hedging** or **netting** depending on the broker; this demo is **hedging** (multiple/opposite positions per symbol allowed).

---

## 2. Account-level capabilities

These are standard MT5 account properties. `account` action returns them all in one JSON object (see section 6.1).

| Capability | MQL5 API | Meaning | Live value (this demo) |
|---|---|---|---|
| Balance | `AccountInfoDouble(ACCOUNT_BALANCE)` | Settled account cash | 9932.25 |
| **Equity** | `AccountInfoDouble(ACCOUNT_EQUITY)` | Balance + floating P/L + swap + commission | 9932.25 |
| Margin used | `AccountInfoDouble(ACCOUNT_MARGIN)` | Funds locked by open positions | 0.00 |
| Free margin | `AccountInfoDouble(ACCOUNT_MARGIN_FREE)` | Available for new positions | 9932.25 |
| Margin level % | `equity / margin * 100` | Health metric (0 when no positions) | 0 |
| Credit | `AccountInfoDouble(ACCOUNT_CREDIT)` | Broker credit line | 0.00 |
| Floating profit | `AccountInfoDouble(ACCOUNT_PROFIT)` | Sum of open-position P/L | 0.00 |
| Leverage | `AccountInfoInteger(ACCOUNT_LEVERAGE)` | Margin multiplier | 1:100 typical |
| Currency | `AccountInfoString(ACCOUNT_CURRENCY)` | Deposit currency | USD |
| Login | `AccountInfoInteger(ACCOUNT_LOGIN)` | Account number | 592065306 |
| Server / broker | `AccountInfoString(ACCOUNT_SERVER / ACCOUNT_COMPANY)` | Trading server / firm | FxPro-MT5 Demo / FxPro Markets Ltd. |
| Trade allowed | `AccountInfoInteger(ACCOUNT_TRADE_ALLOWED)` | Account-level trading permission | true |
| Read-only | (via MCP/terminal) | Server-side block against trading | false |
| Margin mode | `AccountInfoInteger(ACCOUNT_MARGIN_MODE)` | 0 = retail netting, 1 = exchange, 2 = retail hedging | hedging (mode 2 / exchange 1) |
| Account mode | `AccountInfoInteger(ACCOUNT_TRADE_MODE)` | 0 = demo, 1 = contest, 2 = real | demo |

**Server-side rule:** never assume the account is hedging — read `margin_mode` from the `account` snapshot. On a **netting** account there is one position per symbol, so "close_by", multiple opposite positions and per-ticket selection behave differently.

---

## 3. Terminal / EA execution capabilities

| Capability | MQL5 API | Meaning | Live value |
|---|---|---|---|
| Connected | `TerminalInfoInteger(TERMINAL_CONNECTED)` | Server link alive | true |
| Algo trading allowed | `TerminalInfoInteger(TERMINAL_TRADE_ALLOWED)` | Terminal "AutoTrading" button | true |
| EA allowed to trade | `MQLInfoInteger(MQL_TRADE_ALLOWED)` | Per-chart "Allow Algo Trading" of this EA | true (when attached) |
| DLLs allowed | `TerminalInfoInteger(TERMINAL_DLLS_ALLOWED)` | Native DLL call permission | true |
| Web requests allowed | manual URL whitelist: Tools → Options → Expert Advisors → *Allow Web requests* | `ServerURL`/`ProcessedURL` must be listed or WebRequest returns 1001 | must list 127.0.0.1 |

Local guard behaviors you will see as negative retcodes (see section 7):
- **-10** terminal offline · **-11** trading disabled (terminal/account/EA) · **-40** bad pending params · **-42** market closed or no quote for pending.

---

## 4. Symbol-level capabilities (what a symbol can do)

Read with `SymbolInfoInteger/Double(symbol, ...)`. The `trade_order_mode` bit-mask is the single most important value: it tells you **which order types the symbol supports**.

| Capability | MQL5 API | Values | Live example |
|---|---|---|---|
| Trade mode | `SYMBOL_TRADE_MODE` | 0 disabled, 1 long-only, 2 short-only, 3 close-only, **4 full** | **4 (FULL)** on EURUSD, GOLD, WTI, #US30, BITCOIN, AAPL.O, … |
| Execution mode | `SYMBOL_TRADE_EXEMODE` | 0 request, 1 instant, **2 market**, 3 exchange | **2 (market)** everywhere on FxPro |
| Filling mode mask | `SYMBOL_FILLING_MODE` | 1 FOK, 2 IOC, 4 BOC, 8 RETURN | **2 (IOC)** |
| **Order-mode mask** | `SYMBOL_ORDER_MODE` | bit0 buy-limit, bit1 sell-limit, bit2 buy-stop, bit3 sell-stop, bit4 buy-stop-limit, bit5 sell-stop-limit, bit6 close-by | **127 = ALL 6 pending types + close-by** |
| Expiration mask | `SYMBOL_EXPIRATION_MODE` | 1 GTC, 2 DAY, 4 SPECIFIED, 8 SPECIFIED_DAY | **15 = all** |
| Stops level (pts) | `SYMBOL_TRADE_STOPS_LEVEL` | min SL/TP distance from price | FX: 0; SILVER 20; WTI 40; #US30 100; #USNDAQ100 75; BITCOIN 20000; ETHEREUM 1250; GOLD 0 |
| Freeze level (pts) | `SYMBOL_TRADE_FREEZE_LEVEL` | orders frozen near market | 0 |
| Volume min | `SYMBOL_VOLUME_MIN` | smallest lot | 0.01 (GME.N: 20) |
| Volume max | `SYMBOL_VOLUME_MAX` | largest lot | 500 (FX/metals/indices); 1000 (shares); 999999.99 (crypto) |
| Volume step | `SYMBOL_VOLUME_STEP` | lot increment | 0.01 |
| Digits / Point | `SYMBOL_DIGITS` / `SYMBOL_POINT` | price formatting | EURUSD 5/1e-5; USDJPY 3/1e-3; GOLD 2/0.01; indices 2/0.01 |
| Tick size / value | `SYMBOL_TRADE_TICK_SIZE` / `_TICK_VALUE` | price step / value per step | feed reports 0 for FX here → fall back to `SYMBOL_POINT` (the bot already does) |
| Contract size | `SYMBOL_TRADE_CONTRACT_SIZE` | units per lot | FX 100000; GOLD 100; SILVER 5000; WTI/BRENT 1000; indices 1; crypto 1 |
| Calc mode | `SYMBOL_TRADE_CALC_MODE` | forex / cfd / futures / … | forex (FX), cfd (everything else) |
| Spread | `SYMBOL_SPREAD` / `SYMBOL_SPREAD_FLOAT` | current / floating | floating (all) |

**Implication for the server:** because `trade_order_mode = 127`, ALL pending types can be placed on this broker: `buy_limit, sell_limit, buy_stop, sell_stop, buy_stop_limit, sell_stop_limit`. Respect per-symbol `stops_level` when computing SL/TP (Bot_Trader enforces `MinStopDistancePoints` as a floor; broker min distance may be higher — e.g. 20000 pts on BITCOIN).

---

## 5. Position and pending-order capabilities

| Operation | Broker support | Bot_Trader action |
|---|---|---|
| Read open positions (list, ticket, symbol, type, volume, open price, SL, TP, profit, magic, comment) | ✅ | `account` (snapshot `positions[]`) |
| Read pending orders (list, type, state, price, stoplimit, SL, TP, expiration, magic) | ✅ | `account` (snapshot `pending_orders[]`) |
| Open market position | ✅ | `buy` / `sell` |
| Close one position (by ticket or symbol+magic) | ✅ | `close` / `close_position` |
| Close all (magic-filtered) | ✅ | `close_all` |
| Partial close (percentage) | ✅ | `partial_close` |
| Modify SL/TP of a position | ✅ | `modify` |
| Move SL to breakeven | ✅ | `breakeven` |
| Place pending order (all 6 types) | ✅ | see section 6.2 |
| Modify pending order (price/SL/TP/expiration) | ✅ | `modify_pending` |
| Cancel pending order(s) | ✅ | `cancel_pending` |
| Expiring pending orders | ✅ (mask 15) | `expiration` field |
| Close-by (hedged positions) | ✅ (mask includes close-by bit) | not exposed in v6.5 |

---

## 6. Bot_Trader v6.5 commands (server→EA request/response)

### 6.1 Read account state — action `account`

Request (`normalized` object or top level):
```json
{
  "request_id": "status-001",
  "timestamp": "2026-10-07T04:00:00Z",
  "action": "account",
  "symbol": "EURUSD"
}
```
`symbol` is optional and ignored for this action (the EA uses whichever symbol is on the chart).

Response (POST to `ProcessedURL`, same envelope as usual):
```json
{
  "request_id": "status-001",
  "timestamp": "2026-10-07T04:00:00Z",
  "action": "account",
  "symbol": "EURUSD",
  "size": 0.0,
  "status": "SUCCESS",
  "retcode": 0,
  "result": "ACCOUNT SNAPSHOT",
  "snapshot": {
    "account": 592065306,
    "name": "Alex Gitonga",
    "server": "FxPro-MT5 Demo",
    "company": "FxPro Markets Ltd.",
    "currency": "USD",
    "margin_mode": "hedging",
    "trade_mode": "demo",
    "leverage": 100,
    "balance": 9932.25,
    "equity": 9932.25,
    "margin": 0.0,
    "margin_free": 9932.25,
    "margin_level": 0,
    "credit": 0.0,
    "profit": 0.0,
    "trade_allowed": true,
    "trade_expert": true,
    "terminal_connected": true,
    "positions_count": 0,
    "positions": [],
    "pending_orders_count": 0,
    "pending_orders": []
  }
}
```
Each element of `positions[]` looks like:
```json
{"ticket": 4875321, "symbol": "EURUSD", "type": "buy", "volume": 0.10,
 "open_price": 1.12345, "sl": 1.11845, "tp": 0.0, "profit": 12.30,
 "swap": -0.44, "magic": 12345, "comment": "BUY"}
```
Each element of `pending_orders[]` looks like:
```json
{"ticket": 4875399, "symbol": "EURUSD", "type": "buy_limit", "state": "placed",
 "volume": 0.10, "price": 1.11800, "stoplimit": 0.0, "sl": 1.11300,
 "tp": 1.12800, "expiration": "2026.10.31 18:00", "magic": 12345,
 "comment": "PENDING_BUY_LIMIT"}
```

### 6.2 Place a pending order

Six direct actions (all may be plain or with aliases): `buy_limit`, `sell_limit`, `buy_stop`, `sell_stop`, `buy_stop_limit`, `sell_stop_limit`. Or use `action: "pending"` with an explicit `"order_type"`.

Absolute price (default):
```json
{
  "request_id": "po-001",
  "timestamp": "2026-10-07T04:00:00Z",
  "action": "buy_limit",
  "symbol": "EURUSD",
  "size": 0.10,
  "price": 1.11800,
  "stop_loss": 40,
  "take_profit": 80
}
```
Distance pricing — `price` becomes a **signed** number of points from the current market (+ above, − below; buy uses Ask, sell uses Bid):
```json
{
  "request_id": "po-002",
  "action": "sell_limit",
  "symbol": "XAUUSD",
  "size": 0.01,
  "price": 40,
  "price_unit": "points",
  "stop_loss": 20,
  "take_profit": 40
}
```
Stop-limit order (trigger `price`, inner `stoplimit`):
```json
{
  "request_id": "po-003",
  "action": "buy_stop_limit",
  "symbol": "GOLD",
  "size": 0.01,
  "price": 4200.00,
  "stoplimit": 4190.00
}
```
With expiration (ISO or MT5 format):
```json
{
  "request_id": "po-004",
  "action": "pending",
  "order_type": "sell_stop",
  "symbol": "EURUSD",
  "size": 0.10,
  "price": 1.13500,
  "expiration": "2026-10-31T18:00:00"
}
```
`SL/TP` note: a plain number, or an object like `{"unit":"points","value":40}` / `{"unit":"atr","value":1.5}` — for pending orders the distance is measured **from the pending order price**, not the market price. If omitted → no SL/TP on the pending order.

Success ack includes the created ticket:
```json
{"request_id": "po-001", "action": "buy_limit", "symbol": "EURUSD", "size": 0.1,
 "status": "SUCCESS", "retcode": 0, "result": "PENDING buy_limit ticket=4875399",
 "order_ticket": 4875399}
```

### 6.3 Cancel / delete pending orders

By ticket:
```json
{"action": "cancel_pending", "order_ticket": 4875399}
```
By symbol (all matching, magic-filtered):
```json
{"action": "cancel_pending", "symbol": "EURUSD"}
```
All symbols (magic-filtered):
```json
{"action": "cancel_pending", "symbol": "*"}
```
Result: `"ORDER CANCELED ticket=..."` or `"CANCELED 3 ORDERS"`; retcode **-41** if nothing matched or the order belongs to another magic.

### 6.4 Modify a pending order

Only fields present in the payload are changed; omitted fields keep their current values:
```json
{
  "action": "modify_pending",
  "order_ticket": 4875399,
  "price": 1.12050,
  "stop_loss": 45,
  "take_profit": 90,
  "expiration": "2026.10.31 18:00"
}
```
An example ack:
```json
{"action": "modify_pending", "status": "SUCCESS", "retcode": 0,
 "result": "ORDER MODIFIED ticket=4875399", "order_ticket": 4875399}
```

---

## 7. Result codes (read them in the ack `retcode`)

| retcode | Meaning | Category |
|---|---|---|
| 0 | SUCCESS | local |
| -10 | terminal offline | local guard |
| -11 | trading disabled (terminal/account/EA) | local guard |
| -12 | MaxPositions reached (market buy/sell only) | local guard |
| -13 | unknown action | local guard |
| -14 / -15 | market mode != FULL for buy/sell | local guard |
| -20 | risk sizing problem (margin/volume) | local guard |
| -30 / -31 / -32 | no matching position (partial close/modify/breakeven) | local guard |
| **-40** | **invalid pending params (type / price / stoplimit missing)** | local guard (v6.5) |
| **-41** | **no matching pending order, or not owned by this bot's magic** | local guard (v6.5) |
| **-42** | **market closed or no quote for pending placement** | local guard (v6.5) |
| **-43** | **unparseable expiration** | local guard (v6.5) |
| 10004–10069 | broker reject codes (`retcode > 0`, status = FAILED) | broker |
| 10018 | market closed ("requotes"/reject) | broker |
| 10016 / 10017 | invalid stops / missing stop price | broker |

`status` in the ack: `SUCCESS` (0), `FAILED` (>0 broker), `SKIPPED` (<0 local guard).

---

## 8. Suggested server-side implementation checklist

1. **State machine per request:** GET envelope → if `status != "NEW"` or body equals last processed → skip; else parse → POST action → store ack.
2. **Auto-discovery on startup:** send `{"action":"account"}` and cache `margin_mode`, `trade_mode`, `leverage`, `currency`, `margin_level`. Poll it every N minutes or on demand.
3. **Symbol capability probe:** before placing pending orders, read `SYMBOL_ORDER_MODE` (bit-mask), `SYMBOL_EXPIRATION_MODE`, `SYMBOL_TRADE_STOPS_LEVEL`, `SYMBOL_VOLUME_MIN/STEP/MAX` from the snapshot or by an `account` call + a symbol-info request; if mask bit absent → reject that order type.
4. **Magic discipline:** all pending cancel/modify only touch orders whose `magic` equals the EA's `MagicNumber` (default 12345). Never send `cancel_pending` with the wrong magic expectation.
5. **Hedging vs netting:** on netting accounts one position per symbol — do not assume "multiple positions list" semantics. `margin_mode` tells you which.
6. **Distance vs price:** prefer absolute `price` for pending orders sent from server logic; use `price_unit:"points"` only when you intentionally mean "X points from market".
7. **Expiration:** supply ISO (`2026-10-31T18:00:00`) or MT5 (`2026.10.31 18:00`); both accepted. Absent expiration = GTC.
8. **Retries:** dedup is already handled EA-side (same payload skipped). If a POST ack times out, re-read state with `account` before retrying the action.

---

## 9. Enum quick-reference (numeric values your parsers may see)

| ENUM_ACCOUNT_MARGIN_MODE | value |
|---|---|
| ACCOUNT_MARGIN_MODE_RETAIL_NETTING | 0 |
| ACCOUNT_MARGIN_MODE_EXCHANGE | 1 |
| ACCOUNT_MARGIN_MODE_RETAIL_HEDGING | 2 |

| ENUM_SYMBOL_TRADE_MODE | value |
|---|---|
| SYMBOL_TRADE_MODE_DISABLED | 0 |
| SYMBOL_TRADE_MODE_LONGONLY | 1 |
| SYMBOL_TRADE_MODE_SHORTONLY | 2 |
| SYMBOL_TRADE_MODE_CLOSEONLY | 3 |
| SYMBOL_TRADE_MODE_FULL | 4 |

| SYMBOL_ORDER_MODE bits | bit → value |
|---|---|
| buy_limit / sell_limit / buy_stop / sell_stop / buy_stop_limit / sell_stop_limit / close_by | 1,2,4,8,16,32,64 (127 = all) |

| SYMBOL_EXPIRATION_MODE bits | bit → value |
|---|---|
| GTC / DAY / SPECIFIED / SPECIFIED_DAY | 1,2,4,8 (15 = all) |

| SYMBOL_FILLING_MODE bits | bit → value |
|---|---|
| FOK / IOC / BOC / RETURN | 1,2,4,8 (2 = IOC here) |

| ENUM_ORDER_TYPE (pending subset) | value |
|---|---|
| BUY_LIMIT / SELL_LIMIT / BUY_STOP / SELL_STOP / BUY_STOP_LIMIT / SELL_STOP_LIMIT | 2,3,4,5,6,7 |
| ORDER_STATE_PLACED | 4 |

---

## 10. Notes and risks

- Values in section 4 were read live from FxPro MT5 Demo and are **examples, not guarantees** — a different broker/symbol/session can change masks, stops levels or volume limits. Always read at runtime.
- Bot_Trader's risk sizing is a documented approximation (balance-based, crude margin check). `risk`/`percent`/`money` size modes are not exact risk math.
- The bot places pending orders **only** when the symbol trade mode is FULL. If a symbol is CLOSEONLY or closed, expect retcode -42.
- One backtest / one demo run does not prove profitability. Validate pending workflow on the demo account first.
- `Trading_Bot.mq5` mentioned in the old header as a byte-identical twin **does not exist in this terminal**; only `Bot_Trader` is maintained here.