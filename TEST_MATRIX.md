# Test matrix

Two layers: the automated suite (`python -m pytest Trading_Bridge_Source/tests`, 89 tests) pins the contract and the safety rules; the manual procedure below exercises the same rules against a real terminal on a **demo** account.

## Automated suite

| File | Pins down |
|---|---|
| `tests/test_contract.py` | Retcode meanings (including MT5 success codes), action aliases, envelope key order, file-suffix coverage |
| `tests/test_schema_validation.py` | Legacy text, truncated JSON never parsed as legacy, size/distance canonicalization, ATR defaults, breakeven/trailing sub-distances, strict vs permissive, every skipped action refused, malformed ladders warn-only |
| `tests/test_store.py` | FIFO single-use claims, terminal states, expiry, revive rules, dedup window, event ordering, metrics aggregates, the `file_intake` claim register, `inbox_adoption` idempotence and persistence |
| `tests/test_service.py` | The two governing rules: nothing is invented, nothing is sent twice; envelope hygiene (`_meta` never leaves), pause, ack mapping, late ack superseding expiry, preview writes nothing, all-or-nothing settings, retry minting a new id |
| `tests/test_watcher.py` | The inbox is never modified (byte-for-byte snapshot before and after intake), copies marked in the data folder, restart and identical-rewrite both refused a second trade, changed rewrite accepted, corrupt and legacy-text files parked `.invalid` without queueing a trade, duplicate commands, copy-name collisions, interrupted-claim recovery, unreadable inbox tolerated, folder nesting rejected, writes outside the data folder refused; switching the inbox from settings adopts the files already there (and keeps adopting them after they change in place), a file written after the switch trades normally, the first scan after a start does not adopt, adoption survives a restart, `--folder`/env pin outranks the ledger, relative and nested and file-shaped inbox paths rejected |
| `tests/test_web_api.py` | Origin guard (cross-origin 403, board same-origin allowed, non-browser allowed), intake token never echoed, EA cycle over HTTP, dry-run validate, SSE client cap returning 503, the inbox editable over HTTP with all-or-nothing rejection and advertised as a path setting |

## Manual demo procedure

Run each step on a demo account and confirm the board's Signals row, the drawer timeline, the MT5 Experts journal and the broker retcode.

1. **Legacy compatibility** - `{"signal":"BUY SL=50 TP=100 LOT=0.01"}` → queued with a `legacy` warning; symbol comes from the EA's chart.
2. **Structured entry** - `{"request_id":"demo-001","action":"buy","symbol":"EURUSD","size":{"value":0.01,"unit":"lots"},"stop_loss":{"unit":"points","value":200},"take_profit":{"unit":"points","value":400}}` → executed, retcode labelled on the board.
3. **Duplicate protection** - drop the same command under a second filename within the dedup window → `duplicate`, one trade, both copies `.processed`. Re-drop the *identical* file at the same path → skipped by the intake register, no second signal at all.
4. **Idempotency key** - repost the same `request_id` after a cancel → revived row, still one trade.
5. **Refusal** - `{"action":"buy","symbol":"EURUSD"}` in strict mode → `rejected` with the size reason; nothing dispatched.
6. **Corrupt file** - write `{"action": "buy", "sym` → `.invalid` copy, no trade, event on the board, original untouched in the inbox.
7. **Pause** - pause dispatch, drop a valid file, poll → EA receives the idle envelope with `reason: paused`; resume → it dispatches.
8. **Expiry** - pause, dispatch nothing, wait past the dispatch timeout → `expired` with a board toast; **Requeue** puts it back, **Retry as new** mints a fresh id.
9. **Broker rejection** - force a failure (e.g. an impossible volume) → `failed` with the broker retcode and its meaning in the histogram.
10. **Late ack** - acknowledge after expiry → the row becomes `executed`; the trade that happened is the truth.
11. **Settings** - change the watch interval on the board and confirm `/api/v1/watcher` reflects it; submit an out-of-range batch and confirm nothing is applied.
12. **Restart survival** - restart the bridge with a queued signal present → still queued, still dispatched exactly once.
13. **Read-only inbox** - after a mixed batch (good, rejected, corrupt), compare the inbox against a snapshot taken before: same names, same sizes, same mtimes, same bytes. All markings are in `data\intake\`. Optionally deny write permission on the inbox for your user and confirm intake still works.
14. **Folder separation** - start with `--data` pointing at the inbox (or a subfolder of it) → the bridge refuses to start and says why.
15. **Switching the inbox** - put a `.json` file in a second folder, change **Inbox folder (read-only)** on the board and save → the Intake panel shows the new folder, `Adopted on switch` counts the file, and nothing is queued. Rewrite that file with different bytes → still nothing is queued. Drop a new file → it is consumed normally. Restart → the new inbox and the adoption both come back.

## Commands the EA does not execute

`buy_limit`, `sell_limit`, `buy_stop`, `sell_stop`, `close_positions`, `modify_position`, `reverse`, `close_pending` are refused at intake with the reason the EA would have skipped them, so a command can never be acknowledged `SKIPPED` silently.
