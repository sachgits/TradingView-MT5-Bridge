# Universal command and audit behavior

The bridge accepts both compact and rich commands, plus the legacy format:

```json
{
  "symbol": "EURUSD",
  "action": "buy",
  "size": 0.1,
  "stop_loss": {"unit": "points", "value": 200},
  "take_profit": {"unit": "points", "value": 400}
}
```

```json
{
  "signal": "BUY SL=50 TP=100 LOT=0.01"
}
```

## Incomplete JSON and live execution

JSON syntax may be compact, but a live command cannot be semantically incomplete. The bridge rejects commands that lack the fields needed to execute safely, such as an action, symbol for structured entries, valid size, or a required pending-order price. It never invents a symbol, lot size, stop, or direction for a structured live command.

Legacy commands remain compatible and use the EA's configured chart-symbol and default values where the legacy format intentionally omitted them.

## Audit-preserving file workflow

Drop input files into:

```text
C:\Trades\signals\
```

The bridge no longer deletes accepted or rejected files without a record. It moves each file to:

```text
C:\Trades\signals\archive\
```

and adds a `_bridge_audit` object containing:

- `status`: `ACCEPTED`, `REJECTED`, or `INVALID_JSON`
- `received_at`
- `archived_at`
- `error`, when applicable

Accepted commands also receive a generated `request_id` if one was not supplied. The MT5 acknowledgement includes the request ID and source filename. This makes later reporting possible without retaining active input files in the live queue.

Use unique filenames or an explicit `request_id` for every command. The input folder is the queue; the archive is the audit history.
