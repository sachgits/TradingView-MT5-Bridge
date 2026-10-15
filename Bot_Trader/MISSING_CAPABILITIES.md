# Bot_Trader - Missing Server-Side Capabilities & Risk-in-Currency Guide

Companion to `REVIEW_REPORT.md`. Focus: what the *signal server* still cannot express, and how to
express **risk as a cash amount** (e.g. "risk $50") safely.

Reviewed program: `MQL5\Experts\Bot_Trader\Bot_Trader.mq5` (v6.5).

---

## 1. Can the broker "accept risk in dollars"?

**No.** A broker only accepts a **volume** (lots). None of the MT5 order functions have a
"risk" parameter. The $ risk must be converted to lots **before** `OrderSend`, using the symbol's
tick data and the stop-loss distance. That conversion happens in one of two places:

- **In the EA** - send `risk_money` and let `Bot_Trader` size the order (implemented now), or
- **In the server** - compute lots yourself and send `unit:"lots"` (recommended when the server
  already has reliable symbol metadata).

### 1.1 The formula

For a single position with a stop loss `SL` (in points):

```
price_distance_per_lot = SL_points * point            // e.g. 200 * 0.00001 = 0.0020
ticks                  = price_distance_per_lot / tick_size
loss_per_lot           = ticks * tick_value           // account currency lost per 1.0 lot if SL hits
lots                   = risk_money / loss_per_lot
lots                   = NormalizeVolume(lots)        // round to volume_step, clamp min/max
```

Where (MQL5 symbol properties):

| Property | MT5 field |
|---|---|
| `point` | `SYMBOL_POINT` |
| `tick_size` | `SYMBOL_TRADE_TICK_SIZE` |
| `tick_value` | `SYMBOL_TRADE_TICK_VALUE` (account currency, per 1.0 lot, per tick) |

### 1.2 Worked examples

**EURUSD** (5-digit: `point` = 0.00001, `tick_size` = 0.00001, `tick_value` ≈ 1.00):

```
SL = 200 points
price_distance_per_lot = 200 * 0.00001 = 0.0020
ticks = 0.0020 / 0.00001 = 200
loss_per_lot = 200 * 1.00 = $200.00
risk $50  ->  lots = 50 / 200 = 0.25
risk $25  ->  lots = 25 / 200 = 0.125  -> NormalizeVolume -> 0.12
```

**GOLD** (`point` = 0.01, `tick_size` = 0.01, `tick_value` = 1.00, contract 100):

```
SL = 500 points
price_distance_per_lot = 500 * 0.01 = 5.00
ticks = 5.00 / 0.01 = 500
loss_per_lot = 500 * 1.00 = $500.00
risk $50  ->  lots = 50 / 500 = 0.10
```

### 1.3 Currency rule

`risk_money` is interpreted in the **account currency** (here: USD). If the server thinks in another
currency, convert to the account currency **before** sending. The EA has no FX conversion.

### 1.4 Data-quality warning (important)

The conversion depends entirely on `tick_value` / `tick_size`. In this terminal's Market Watch
snapshot these came back as **0** for EURUSD, which makes the EA fall back to
`tickSize = point` and `tickValue = 10.0`. That fallback assumes **$10 per POINT**, which is the
value of a **PIP** for a 1.0 lot, i.e. **10x too large** for a 5-digit FX pair. Consequences:

- FX risk sizing computed with the fallback is **~10x too small**.
- Metals/CFDs (GOLD) generally have correct tick_value, so they are unaffected.

**Recommendation:** when reliability matters, compute lots on the server from live symbol metadata
and send `unit:"lots"`. Treat the EA-side `risk_money` mode as a convenience/approximate feature
(the EA header already calls all sizing an "approximation").

---

## 2. `risk_money` - now supported by Bot_Trader

### 2.1 New request field / size unit

```json
{ "action": "buy", "symbol": "GOLD",
  "size": { "value": 50, "unit": "risk_money" },
  "stop_loss":   { "type": "distance", "unit": "points", "value": 500 },
  "take_profit": { "type": "distance", "unit": "points", "value": 1000 } }
```

Accepted unit aliases: `risk_money`, `risk_cash`, `risk_amount`, `risk_usd`.
Alternative form: keep `size.unit` as a normal mode and send a top-level `risk_money` field:

```json
{ "action": "buy", "symbol": "GOLD", "size": { "value": 0, "unit": "risk_money" },
  "risk_money": 50,
  "stop_loss": { "type": "distance", "unit": "points", "value": 500 } }
```

If both are present, `size.value` wins; otherwise the top-level `risk_money`
(or `risk_cash` / `risk_amount`) is used.

### 2.2 Behaviour

- The EA computes `slPoints` first (points / price_distance / ATR all work), then
  `lots = risk_money / loss_per_lot`, then normalises to `volume_step` and clamps to `volume_min`.
- **A stop loss is required** for this mode. Without SL, `slPoints = 0`, `loss_per_lot = 0` and the
  EA falls back to the `LotSize` input (documented default) - i.e. the cash risk is ignored. The
  server should always send an SL with `risk_money`.
- The existing `MaxExposurePerSymbol` (lot cap) and `MaxOpenRiskPercent` (notional % cap) still apply
  afterwards and can reduce the size.
- `unit:"risk"` (percent of balance) is unchanged; `unit:"percent"` (notional %) and
  `unit:"money"` (notional cash) are unchanged.

### 2.3 Size-mode summary

| `size.unit` | Meaning | Input source |
|---|---|---|
| `lots` (default) | fixed lots | `size.value` |
| `percent` | % of balance as notional | `size.value` |
| `money` / `currency` | cash notional | `size.value` |
| `risk` | % of balance risked if SL hits | `size.value` (+ `risk_percent`) |
| `risk_money` / `risk_cash` / `risk_amount` / `risk_usd` | **cash risked if SL hits** | `size.value` or `risk_money` |

---

## 3. Other capabilities missing from the server end (roadmap)

Grouped by theme. None of these are implemented; they are proposals.

### 3.1 Execution control (request fields)

| Capability | Suggested field | Note |
|---|---|---|
| Max slippage / deviation | `deviation` (points) | `CTrade::SetDeviationInPoints`; currently fixed |
| Order comment / strategy tag | `comment`, `strategy_id`, `client_tag` | echoed into the deal comment for reconciliation |
| Time-in-force for market orders | `time_in_force` | market orders have no TIF; only pending do |
| Reduce-only | `reduce_only` | reject if the order would increase exposure |
| OCO / bracket group | `oco_group` | sibling SL/TP orders cancelled together |
| Partial-close **by volume** | `close_volume` | currently only `close_percent` |
| Close/modify **by position ticket** | `position_ticket` | currently symbol+magic only |
| Trailing stop execution | `trailing` (object) | parsed but NOT executed |
| Breakeven trigger enforcement | `breakeven.trigger` | parsed but NOT enforced (moves immediately) |
| Dry run / validation only | `dry_run` | return the computed plan without sending |

### 3.2 Risk governance

| Capability | Note |
|---|---|
| Daily loss limit / kill switch | stop accepting signals after -X% day |
| Max concurrent risk (portfolio) | sum of open risk vs a ceiling |
| Equity-based risk (`% of equity`, not balance) | currently balance only |
| Per-symbol exposure in cash | `MaxExposurePerSymbol` is lots only |
| Margin pre-check before sending | return available margin and required margin |
| Correlation/exposure caps | avoid stacked correlated risk |

### 3.3 Feedback to the server (see REVIEW_REPORT.md §4)

- `position_ticket` / `deal_ticket` / `filled_price` / `filled_volume` after market fills.
- `sl` / `tp` actually applied; `remaining_volume` after partial close.
- `closed[]` / `cancelled[]` lists for bulk actions.
- `stage` + `errors[]` + `schema_version` for machine parsing.
- A response **at all** on parse/symbol rejection (currently silent).

### 3.4 Protocol / operations

| Capability | Note |
|---|---|
| Idempotency key | dedup is byte-exact today; a stable `signal_id` would make it robust |
| At-least-once vs exactly-once semantics | define retry/ack contract explicitly |
| Symbol metadata endpoint | let the server fetch point/tick_size/tick_value/volume_step to size orders itself |
| Trading-hours / session awareness | the EA only reads the static `SYMBOL_TRADE_MODE` |
| Structured reject codes in the response | one code per reason (already have local codes) |
| Schema-version negotiation | `schema_version` is currently ignored; add a compatibility check |
| Health/heartbeat endpoint | EA logs a heartbeat; the bridge has no queryable liveness |
| Multi-account routing | one EA instance = one account today |

---

## 4. Guide for the maintenance team

### 4.1 To use cash-risk sizing

1. Send `size.unit` = `risk_money` (or `risk_cash` / `risk_amount` / `risk_usd`) with
   `size.value` = the cash amount in the **account currency**.
2. **Always include a stop loss** in the same payload (points, price_distance or atr).
3. Expect the final volume to be rounded to `volume_step` and floored to `volume_min`; verify with
   the returned `size` in the result body.
4. Validate on DEMO first.

### 4.2 When to size on the server instead

If the server has (or can fetch) live `tick_value` / `tick_size`, compute lots with the formula in
§1.1 and send `unit:"lots"`. This avoids the `tick_value = 10.0` fallback error on FX.

### 4.3 Checks to run after deploying

| Check | Expected |
|---|---|
| `risk_money: 50`, EURUSD, SL 200 pts | ~0.25 lots (if tick_value is correct) |
| `risk_money: 50`, GOLD, SL 500 pts | ~0.10 lots |
| `risk_money` with no SL | falls back to `LotSize` input, log a warning |
| `risk_money` below one step | clamped up to `volume_min` |
| Missing/zero `tick_value` | size is wrong -> prefer server-side lots |

### 4.4 Related fixes already applied (see REVIEW_REPORT.md)

- `NormalizeVolume` no longer forces `volume_min` (sizing works at all now).
- `modify` / `breakeven` / `partial_close` / `close_all` now report the real broker result.
- `cancel_pending` returns the cancelled `order_ticket`.
- ATR SL/TP no longer silently degrades to 50/50.

### 4.5 Still open / risk notes

- Sizing remains a **documented approximation**; margin is only sanity-checked.
- No guarantee of profitability - this is order plumbing, not a strategy. Backtest on demo.
- The EA cannot convert between currencies; `risk_money` must be in the account currency.
- `MaxOpenRiskPercent` (default 3% notional) can silently cap the computed size; review that input
  when using cash-risk mode.
