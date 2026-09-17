# Demo test matrix

Run these examples one at a time on a demo account. Confirm the bridge log, the archived JSON, the MT5 Experts log, symbol, volume, stop/target prices, and broker retcode after each test.

## 1. Legacy compatibility

```json
{"signal":"BUY SL=50 TP=100 LOT=0.01"}
```

Expected: legacy parsing; symbol comes from the legacy EA chart configuration. Do not use this format for multi-symbol routing.

## 2. Minimal structured FX entry

```json
{"schema_version":"2.0","request_id":"demo-fx-001","symbol":"EURUSD","action":"buy","size":0.01,"stop_loss":{"unit":"points","value":200},"take_profit":{"unit":"points","value":400}}
```

## 3. Short FX entry

```json
{"request_id":"demo-fx-002","symbol":"USDJPY","action":"sell","size":{"value":0.01,"unit":"lots"},"stop_loss":{"unit":"points","value":250},"take_profit":{"unit":"points","value":500}}
```

## 4. Metal with price distance

```json
{"request_id":"demo-metal-001","symbol":"XAUUSD","action":"buy","size":0.01,"stop_loss":{"unit":"price_distance","value":3.0},"take_profit":{"unit":"price_distance","value":6.0}}
```

## 5. Index with ATR stop

```json
{"request_id":"demo-index-001","symbol":"US500","action":"sell","size":0.01,"stop_loss":{"unit":"atr","period":14,"timeframe":"M15","multiplier":2.0},"take_profit":{"unit":"atr","period":14,"timeframe":"M15","multiplier":4.0}}
```

Use your broker's exact symbol, which may be `US500.cash`, `SPX500`, or another name.

## 6. Stock percentage distances

```json
{"request_id":"demo-stock-001","symbol":"AAPL","action":"buy","size":1,"stop_loss":{"unit":"percent","value":1.0},"take_profit":{"unit":"percent","value":2.0}}
```

## 7. Multiple targets and protection plan

```json
{"request_id":"demo-management-001","symbol":"EURUSD","action":"buy","size":0.02,"stop_loss":{"unit":"points","value":200},"take_profit":{"unit":"points","value":600},"take_profits":[{"distance":{"unit":"points","value":200},"close_percent":50},{"distance":{"unit":"points","value":400},"close_percent":50}],"breakeven":{"enabled":true,"trigger":{"unit":"points","value":150},"lock_in":{"unit":"points","value":20}},"trailing":{"enabled":true,"activation":{"unit":"points","value":250},"distance":{"unit":"points","value":100}}}
```

This validates the command contract. Continuous TP/BE/trailing execution must be verified against the compiled EA implementation before live use.

## 8. Pending order validation

```json
{"request_id":"demo-pending-001","symbol":"EURUSD","action":"buy_limit","price":1.05000,"size":0.01,"stop_loss":{"unit":"points","value":200},"take_profit":{"unit":"points","value":400}}
```

## 9. Selective close command shape

```json
{"request_id":"demo-close-001","action":"close_position","position_ticket":"123456789"}
```

Use a real demo ticket only. The current bridge can validate the command shape; confirm the compiled EA implements the requested management action before relying on it.

## 10. Invalid-command safety tests

These must be rejected and archived as `REJECTED`:

```json
{"action":"buy"}
{"symbol":"EURUSD","action":"buy","size":0}
{"symbol":"EURUSD","action":"buy_limit","size":0.01}
{"symbol":"EURUSD","action":"buy","size":0.01,"take_profits":[{"distance":{"unit":"points","value":200},"close_percent":110}]}
```
