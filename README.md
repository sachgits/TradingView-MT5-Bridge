# TradingView to MT5 Trade Bridge

**FREE & Open Source** - Automated trading system that detects trades from TradingView Strategy Tester (or any JSON producer) and executes them on MetaTrader 5 through the **Bot_Trader** EA, with a full audit ledger and a live **signal board** in your browser.

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![Python](https://img.shields.io/badge/Python-3.9+-blue.svg)](https://www.python.org/)
[![MT5](https://img.shields.io/badge/MT5-Bot__Trader__v6.5-green.svg)](https://www.metatrader5.com/)

---

## ⚠️ RISK WARNING — READ BEFORE USE

This software executes real trades on your broker account automatically. Automated trading carries significant financial risk. You can lose your entire account balance, including amounts beyond your initial investment.

- The author is NOT responsible for any financial losses, account liquidations, margin calls, or damages of any kind resulting from use of this software.
- This is NOT financial advice and NOT a trading strategy. It is a technical automation tool only.
- Past performance in TradingView's Strategy Tester does NOT guarantee future results on a live account.
- Slippage, broker requotes, network latency, and platform outages can cause execution to differ significantly from what the backtester shows.
- Verify that automated/algorithmic trading is permitted by your broker's Terms of Service before use.
- Always test on a demo account first. Even after demo testing, live results may differ substantially.

By using this software, you accept full and sole responsibility for all trading decisions and their financial consequences.

---

## 🌟 Why This Project is Different

- ✅ **100% FREE** - no TradingView Premium, no webhooks, no cloud VPS, no subscription
- ✅ **Three input methods** - Chrome extension, HTTP API, or JSON files dropped in a folder
- ✅ **A signal board, not a log window** - every command, dispatch, acknowledgement, retcode and latency figure is stored in a SQLite ledger and streamed to `http://127.0.0.1:8080/`
- ✅ **Live account view** - a Positions tab pulls balance, equity, margin, open positions and pending orders straight from MT5 (the EA reports a snapshot on an idle poll) and shows them in the browser
- ✅ **Pending orders** - `buy_stop`, `buy_limit`, `sell_stop`, `sell_limit`, `buy_stop_limit`, `sell_stop_limit`, `cancel_pending` and `modify_pending`, with absolute or signed-distance entry prices and expirations
- ✅ **Idempotent by design** - a command is never dispatched twice, even across a bridge restart
- ✅ **Auditable file intake** - the signal folder is read-only: files are copied into the bridge's own data folder and the copy is marked `.processed` / `.rejected` / `.invalid`. Nothing in the shared folder is renamed or deleted, and you can point the bridge at a different folder from the board without restarting
- ✅ **Production server** - Waitress WSGI, thread-safe queue, bounded SSE streams

---

## 📖 How It Works

```
TradingView Strategy Tester ──▶ Chrome extension ──┐
Any bot / webhook / script  ──▶ JSON file in inbox ─┤──▶ TradingBridge (localhost:8080)
Operator or test harness    ──▶ HTTP API ───────────┘      │  read inbox (never write to it)
                                                           │  copy → normalize → validate → queue
                                                           │  SQLite ledger + event bus
                                                           │  signal board (SSE)
                                                    MT5 Bot_Trader EA polls GET /signal
                                                           │  executes, POSTs the outcome
                                                    ◀──────┘  board shows the result per trade
```

Two folders are involved and they are not interchangeable: the **inbox** holds
commands from you and any other application and is read-only as far as the bridge
is concerned, while the **data folder** is the bridge's own and holds the ledger
plus a marked copy of every file it consumed. The inbox is the one path you can
change while the bridge runs - it is a field on the board's Settings tab - and
whatever is already in a folder you switch to is adopted rather than traded.

Every command that enters the bridge gets a `request_id` and a ledger row. The EA polls for the oldest queued command, executes it, and posts the outcome (status, retcode, result text) back. The board ties the two together: one row per trade, with its dispatch time, acknowledgement, retcode meaning and full event timeline.

---

## 🛠️ Installation

### 1. TradingBridge

- **Pre-built:** run `Trading_Bridge.exe`, allow network access.
- **From source:** `python Trading_Bridge_Source/run_bridge.py` (add `--no-gui` for headless, `--port`, `--host`, `--folder`, `--data` to override).

Then open **http://127.0.0.1:8080/** - that is the signal board.

### 2. Choose your input method

**🔹 Chrome extension (TradingView users)**
1. Chrome → `chrome://extensions/` → enable **Developer mode**
2. **Load unpacked** → select `Tradingview_Trade_Detector-Extension`
3. Open TradingView → Strategy Tester → **List of Trades**
4. Site settings → allow **Local Network Access** for tradingview.com

**🔹 JSON files (custom bots)**
1. Write commands to the inbox (default `C:\Trades\trade_journal`). It is **read-only** to the bridge: your files are never renamed, marked or deleted
2. Each one is copied into the data folder (default `C:\Trades\TradingBridge\intake`) and the copy is marked `.processed`, `.rejected`, `.invalid` or `.interrupted`
3. What has already been consumed is remembered in the ledger, so a file left in the inbox cannot produce a second trade
4. Trade-journal records (`pair`/`direction`/`setup`) are recognised and reduced to a lean EA command; a record whose trigger is older than `journal_max_age_seconds` is refused as history rather than traded. See [FILE_IO_GUIDE.md → Trade-journal records](FILE_IO_GUIDE.md#trade-journal-records)
5. See [FILE_IO_GUIDE.md](FILE_IO_GUIDE.md)

**🔹 HTTP API** - `POST /api/v1/signals` with any command Bot_Trader accepts (see below).

### 3. MT5 EA setup

1. Copy the `Bot_Trader` folder to `C:\Users\<you>\AppData\Roaming\MetaTrader 5\MQL5\Experts\`
   (`Trading_Bot` is kept as an identical second copy; compile either in MetaEditor)
2. MT5 → Tools → Options → Expert Advisors → add `http://127.0.0.1:8080` to the allow-list
3. Attach the EA to a chart, tick **Allow Algo Trading**, and enable the Algo Trading button

---

## 🧭 The signal board

| Tab | What it shows |
|---|---|
| **Board** | KPI cards (total / executed / failed / skipped / waiting / rejected / expired), throughput chart, outcome mix, by-symbol and by-action bars, latency percentiles, retcode histogram, live event feed |
| **Signals** | The full ledger with search and status/symbol/action filters; click a row for the detail drawer with the command, the raw payload, the EA's acknowledgement and the event timeline |
| **Positions** | The live account snapshot the EA reported: balance / equity / margin / free-margin / margin-level KPIs, plus a table of open positions and one of pending orders, each with its own P/L. **Refresh** forces a new snapshot on the next idle poll |
| **Events** | Every event the bridge emitted, newest first, with follow-along scrolling |
| **Send** | Compose a command, validate it dry-run (same checks as a real submission, writes nothing), preview the exact envelope the EA will receive, then send |
| **Settings** | Watch interval, file settle time, intake on/off, validation mode, queue depth, signal TTL, dedup window, dispatch timeout, account snapshot interval, pause, origin check - saved and applied live. Typed values survive the board's background refresh; the form is only rebuilt when you save or discard |
| **EA contract** | What Bot_Trader v6.5 actually implements: executed actions, skipped actions, size and distance units, inputs, envelope keys, retcode tables |

The board updates over Server-Sent Events. If a stream cannot be opened (too many tabs, or a proxy that buffers), it falls back to polling automatically - every event is durable in the ledger either way, so nothing is missed.

Top-bar controls: **Pause dispatch** (the EA keeps polling but receives nothing), **Scan folder** (immediate intake pass), **Clear queue** (cancel everything not yet sent).

---

## 📐 Command vocabulary (schema v2.1)

Anything Bot_Trader v6.5 accepts is accepted here. Legacy plain-text signals (`BUY SL=50 TP=100 LOT=0.01`) and `{"signal": "..."}` envelopes still work and are flagged as legacy on the board.

```json
{
  "action": "buy",
  "symbol": "EURUSD",
  "size": { "value": 0.01, "unit": "lots" },
  "stop_loss": { "unit": "points", "value": 200 },
  "take_profit": { "unit": "points", "value": 400 }
}
```

- **Actions:** market `buy`, `sell`; management `close_position`, `close_all`, `partial_close`, `modify`, `breakeven`, `hold`; pending `buy_stop`, `buy_limit`, `sell_stop`, `sell_limit`, `buy_stop_limit`, `sell_stop_limit`, `cancel_pending`, `modify_pending`; plus `account` for a read-only snapshot
- **Size:** `lots`, `percent`, `risk_percent`-style risk sizing via `risk_percent`; units the EA cannot honour exactly are reported as warnings, never guessed
- **Distances:** `points`, `pips`, `price_distance`, `price`, `atr` (ATR timeframe is parsed but the EA uses its chart timeframe - flagged as a warning)
- **Pending entry:** `order_type` (implied by the action), `price` with `price_unit` (`price` absolute, `points`/`distance` signed offset from Ask/Bid), `stoplimit` for the stop-limit pair, `expiration` (ISO or MT5 datetime), and SL/TP measured from the *order* price. `cancel_pending` / `modify_pending` target an `order_ticket`, or a symbol + the bot's magic
- **Aliases** accepted and canonicalized: `lot`/`volume`/`qty`, `sl`/`stoploss`, `tp`/`takeprofit`, `entry`/`stop_limit`/`ticket`/`expiry`, and legacy keyword text

The bridge **never invents a trade parameter**. If a size, symbol or stop is missing where the EA would substitute its own input, strict validation refuses the command instead of letting the EA guess.

Full field reference: [AUDIT_AND_COMMANDS.md](AUDIT_AND_COMMANDS.md).

---

## 🛡️ Safety rules the bridge enforces

1. **Never invent a trade parameter.** Missing or ambiguous size/symbol/direction is a rejection, not a default.
2. **Never send the same command twice.** Commands are deduplicated by content hash and by `request_id`; the ledger survives restarts, so a replayed file or a retried POST cannot produce a second trade.
3. **An unacknowledged dispatch is never silently resent.** If the EA does not acknowledge within the dispatch timeout the signal becomes `expired` with a loud board entry; retrying mints a new `request_id` on purpose.
4. **A late acknowledgement wins.** If the EA reports success after the bridge gave up waiting, the ledger records the trade that actually happened.
5. **Corrupt input never trades.** A truncated or non-JSON `.json` file is recorded as `invalid` and its copy parked `.invalid`; it is never offered to the legacy text parser.
6. **The inbox belongs to its producers.** The bridge reads it and never writes to it - no renames, no markings, no deletions - so other applications sharing that folder see their files exactly as they left them.
7. **Everything is auditable.** Rejected and invalid commands get ledger rows and events too - the board shows what was refused, not only what traded.

---

## 🔌 Endpoints

**EA-facing** (no `Origin` header, so the origin check does not apply):
`GET /signal` · `POST /signal/processed` · `POST /signal` (legacy) · `POST /signal/clear` · `GET /health`

**Operator / API:**
`GET|POST /api/v1/signals` · `GET /api/v1/signals/<id>` · `POST /api/v1/signals/<id>/cancel|retry|requeue` · `POST /api/v1/validate` · `GET /api/v1/events` · `GET /api/v1/metrics` · `GET|PATCH /api/v1/settings` · `GET /api/v1/contract` · `GET /api/v1/watcher` · `GET /api/v1/state` · `POST /api/v1/rescan` · `GET /api/v1/account` · `GET /api/v1/positions` · `POST /api/v1/account/refresh` · `GET /api/v1/stream` (SSE)

---

## 🔒 Security

- The bridge binds to `127.0.0.1` by default. Loopback is not a trust boundary by itself, so intake routes additionally require an allowed `Origin` (TradingView, or the board's own origin) when the request comes from a browser.
- Set `intake_token` (Settings tab or `INTAKE_TOKEN`) to require an `X-Bridge-Token` header on every intake route. The token is masked in all API responses.
- Request bodies are capped at 1 MiB.

---

## 🧪 Tests and build

```powershell
python -m pip install -r Trading_Bridge_Source/requirements.txt
python -m pytest Trading_Bridge_Source/tests      # 167 tests: contract, schema, store, service, watcher, pending/account, HTTP
powershell -File build.ps1                          # PyInstaller onefile + distributable zip
```

---

## 📚 Documentation

- [INSTALL_AND_TEST_GUIDE.md](INSTALL_AND_TEST_GUIDE.md) - setup and end-to-end test procedure
- [FILE_IO_GUIDE.md](FILE_IO_GUIDE.md) - folder intake, file markings, examples
- [AUDIT_AND_COMMANDS.md](AUDIT_AND_COMMANDS.md) - command schema, ledger fields, endpoints
- [TEST_MATRIX.md](TEST_MATRIX.md) - the behaviours the test suite pins down
- [DISCLAIMER.md](DISCLAIMER.md) - the risk warning in full

## License

MIT - see [LICENSE](LICENSE).
