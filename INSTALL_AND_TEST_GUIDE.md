# Universal TradingBridge — Installation, migration, and operation

## What changed from the original bot

The original EA accepted a flat signal such as `BUY SL=50 TP=100 LOT=0.01`, used the chart symbol, and used account-wide position checks. The feature branch adds a structured-command intake layer, legacy normalization, validation, symbol fields, universal distance units, selective-management command vocabulary, and an archive-oriented file workflow.

The current branch is a staged refactor. The Python bridge and command contract are usable for intake/testing, but the checked-in `.ex5` is a compiled binary from the older EA and must not be treated as the v6 structured-command EA.

## Build the Windows executable

Run PowerShell from the repository root on Windows:

```powershell
Set-ExecutionPolicy -Scope Process Bypass
.\build.ps1
```

The script creates `TradingBridge-Universal.zip`. It builds the Python bridge executable and packages the source, schema, EA source, examples, documentation, and disclaimer. The build requires Python 3 and internet access for dependency installation.

Do not copy the old `Trading_Bridge.exe` over the new executable. Use the executable produced by `build.ps1`.

## MT5 migration: replace the old EA

Yes. Compile and replace the old EA before testing structured commands:

1. Stop the old EA on every chart where it is attached.
2. Copy `Trading_Bot\Trading_Bot.mq5` into the MT5 `MQL5\Experts\Trading_Bot\` folder.
3. Open it in MetaEditor and compile it.
4. Check the compile result and fix any MetaEditor errors before proceeding.
5. Attach the newly compiled EA to a demo chart.
6. Enable Algo Trading and allow `http://127.0.0.1:8080` under MT5 WebRequest settings.
7. Do not use the repository's existing `Trading_Bot.ex5` as proof that the new source is installed; it must be rebuilt from the source.

Keep the old EA binary backed up, but do not run both old and new EAs against the same command endpoint or they can consume/duplicate commands.

## Run the bridge

1. Unzip `TradingBridge-Universal.zip`.
2. Run `Trading_Bridge.exe`.
3. Confirm the GUI shows the input and archive folders.
4. The default input folder is `C:\Trades\signals`.
5. The default audit folder is `C:\Trades\signals\archive`.
6. Place completed `.json` files in the input folder; do not write files directly while they are being read. Use a temporary filename and rename to `.json` after writing.

Environment overrides:

```powershell
$env:TRADING_BRIDGE_SIGNALS_FOLDER = 'D:\Trading\signals'
$env:TRADING_BRIDGE_ARCHIVE_FOLDER = 'D:\Trading\signal-archive'
$env:TRADING_BRIDGE_CHECK_INTERVAL = '0.5'
```

## Archive and status behavior

Input files are retained as audit records by moving them to the archive. They are not silently deleted. Current intake statuses include `ACCEPTED`, `REJECTED`, and `INVALID_JSON`. A bridge acknowledgement means queued/accepted; it does not by itself prove a broker fill.

For reliable reporting, the next execution-feedback enhancement should update the archived record after MT5 returns a broker retcode. Until that exists, report bridge acceptance separately from broker execution.

## Safe operating rule

Test on demo first. A syntactically valid JSON document is not necessarily a safe trade command. Structured entries require an action, symbol, and valid size; pending orders require a price. The validator rejects semantically incomplete live commands instead of inventing missing values.
