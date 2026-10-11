# Installation, migration and operation

## Components

| Component | Where | Role |
|---|---|---|
| `Tradingview_Trade_Detector-Extension` | Chrome | Watches TradingView's List of Trades and posts commands |
| `Trading_Bridge_Source/run_bridge.py` + `bridge/` | Python 3.9+ | Intake, validation, queue, ledger, HTTP API, SSE, signal board |
| `Trading_Bridge_Source/web/` | served at `/` | The signal board (vanilla HTML/CSS/JS, no build step) |
| `Bot_Trader/Bot_Trader.mq5` (v6.5) | MT5 | Executes commands and reports every outcome back |
| `Trading_Bot/Trading_Bot.mq5` | MT5 | Identical second copy; keep the two in sync |

## Run from source

```powershell
python -m pip install -r Trading_Bridge_Source\requirements.txt
python Trading_Bridge_Source\run_bridge.py            # GUI + server
python Trading_Bridge_Source\run_bridge.py --no-gui --port 8080 --folder C:\Trades\trade_journal
```

Flags: `--no-gui`, `--host`, `--port`, `--folder`. Environment overrides (`SIGNALS_FOLDER`, `WATCH_INTERVAL`, `VALIDATION_MODE`, `MAX_QUEUE_DEPTH`, `SIGNAL_TTL_SECONDS`, `DEDUP_WINDOW_SECONDS`, `DISPATCH_TIMEOUT_SECONDS`, `PAUSED`, `INTAKE_TOKEN`, `REQUIRE_ORIGIN`, `DB_PATH`) are all mirrored on the board's Settings tab, which persists them in the ledger database.

Open **http://127.0.0.1:8080/** for the signal board.

## Build the Windows executable

```powershell
Set-ExecutionPolicy -Scope Process Bypass
.\build.ps1
```

Produces `dist\Trading_Bridge.exe` (PyInstaller onefile, signal board bundled via `--add-data`) and `TradingBridge-Universal.zip` with source, tests, EA sources, examples and docs. Do not reuse an older `Trading_Bridge.exe`: the entry point moved from `bridge.py` to `run_bridge.py` and the web assets must travel inside the bundle.

## MT5 setup

1. Stop any older EA attached to charts.
2. Copy `Bot_Trader` (or `Trading_Bot`) into `MQL5\Experts\` and compile it in MetaEditor. The repository's old `.ex5` binaries are **not** the v6.5 structured-command EA; always rebuild from source.
3. MT5 → Tools → Options → Expert Advisors → allow `http://127.0.0.1:8080`.
4. Attach to a demo chart, tick **Allow Algo Trading**, enable the Algo Trading button.
5. Never run two EAs against the same bridge: they would compete for the same queue.

## File intake

- Two folders. The **inbox** (default `C:\Trades\trade_journal`) holds commands and is shared with your other producers - the bridge only reads it. The **data folder** (default `C:\Trades\TradingBridge`) is the bridge's own and holds the ledger, `intake/` and `interrupted/`. Set them with `--folder` / `--data` (or `TRADING_BRIDGE_SIGNALS_FOLDER` / `TRADING_BRIDGE_DATA_FOLDER`); the data folder needs a restart, the inbox does not - it is the first field on the board's Settings tab.
- The bridge refuses to start if the two are the same folder, or if either is nested inside the other. The same check rejects a bad inbox typed into the board.
- When the inbox is switched from the board, every `.json` file already in the new folder is **adopted** - recorded by path and never traded - so pointing the bridge at a folder of history cannot fire a trade per file. A file written after the switch is a normal command.
- Write to a temporary name and rename to `.json` when complete; the watcher also waits `file_settle_ms` after the last write.
- Your file is left exactly as it was. The bridge copies it into `data\intake\` and marks **the copy** `.processed` / `.rejected` / `.invalid`. Nothing is deleted and nothing is written into the inbox.
- What has already been consumed is recorded permanently in the ledger's `file_intake` table, keyed on path plus content hash, so a file that stays in the inbox for a week cannot produce a second trade - including across a restart.
- A claim left open by a crash is closed as `interrupted` at startup, its copy moved to `data\interrupted\`, and it is **not** replayed automatically - replaying is exactly the double execution the ledger exists to prevent. Re-submit it yourself once you have checked the broker side.

**Upgrading from a build that marked files in place.** Your existing ledger is kept: the new `file_intake` and `inbox_adoption` tables are added on first start and `PRAGMA user_version` moves to 3. Files an older build already renamed to `.processed` / `.rejected` / `.invalid` are ignored because only `.json` is read, so nothing is consumed twice. Anything still named `.json` in that folder will be consumed once by the new build - move it out first if you do not want that, or switch to that folder from the board and it will be adopted instead. You can then move the old marked files into `data\intake\` yourself, or delete them; the ledger remains the record.

## Verifying an installation end to end

1. Start the bridge; the board shows `live` (SSE) or `polling` (fallback) in the top bar.
2. Send tab → load the `buy` example → **Validate** (dry run) → **Send to MT5**. The drawer opens on the queued row.
3. With the EA attached, the row moves `queued → dispatched → executed` and a toast reports the retcode. Without the EA, the top bar shows `EA OFFLINE` and the row expires after the dispatch timeout - both are visible, neither is silent.
4. Drop a JSON file into the inbox and confirm the original is untouched there, `data\intake\` gains a `.processed` copy, and the signal appears with `source: file`.
5. `python -m pytest Trading_Bridge_Source\tests` - all green.

## Safe operating rules

- Demo first, always.
- Strict validation is the default: a structured entry without an explicit size is refused rather than sized by the EA's inputs.
- Pause dispatch before changing EA settings or symbols on a live chart.
- A bridge acknowledgement of `queued` or `dispatched` is not a broker fill; only the EA's acknowledgement (`executed` with a retcode) is.
