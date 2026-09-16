# File I/O Mode Guide

This guide explains how to use the **File I/O mode** of TradingBridge, which allows you to send trading signals via JSON files instead of relying on the TradingView Chrome extension.

---

## Overview

**File I/O mode** allows you to:
- Drop JSON signal files into a monitored folder
- Trigger trades from any source (custom bot, webhook, script, etc.)
- No TradingView Premium or Chrome extension needed
- Full backward compatibility with HTTP POST method

---

## Setup

### 1. Folder Configuration

The bridge watches this folder for signal files:
```
C:\Trades\signals\
```

**To change the folder path:**
- Edit `Trading_Bridge_Source/bridge.py`
- Modify this line:
  ```python
  SIGNALS_FOLDER = r"C:\Trades\signals"  # Change this path
  ```

### 2. Start the Server

Run `TradingBridge.exe` or:
```bash
python Trading_Bridge_Source/bridge.py
```

The server will:
- Create the signals folder if it doesn't exist
- Start watching for JSON files
- Log all activity in the GUI

---

## JSON Signal Format

### Full Format (with SL/TP/LOT)

```json
{
  "signal": "BUY SL=50 TP=100 LOT=0.01",
  "timestamp": "2025-09-16T14:30:45Z"
}
```

### Simple Format (side only)

```json
{
  "signal": "SELL"
}
```

The bridge will use default SL/TP/LOT from MT5 EA inputs if not specified.

### Field Reference

| Field | Type | Required | Example | Notes |
|-------|------|----------|---------|-------|
| `signal` | string | ✅ Yes | `"BUY SL=50 TP=100 LOT=0.01"` | Format: `BUY\|SELL [SL=pips] [TP=pips] [LOT=size]` |
| `timestamp` | string | ❌ Optional | `"2025-09-16T14:30:45Z"` | ISO 8601 format; server generates if omitted |

---

## How It Works

```
Your Signal Source
    ↓
Create JSON file in C:\Trades\signals\
    ↓
Bridge detects file (checks every 2 seconds)
    ↓
Reads and validates JSON
    ↓
Sets signal to "NEW" status
    ↓
MT5 EA polls /signal, sees "NEW"
    ↓
EA executes trade, calls /signal/processed
    ↓
Bridge marks signal as "PROCESSED"
    ↓
Bridge deletes the JSON file
```

**Total latency: ~2.75 seconds** (2s folder check + 0.75s MT5 poll)

---

## Examples

### Example 1: Python Bot Sending Signals

```python
import json
from pathlib import Path
from datetime import datetime
import time

SIGNALS_FOLDER = Path(r"C:\Trades\signals")

def send_signal(side, sl_pips=None, tp_pips=None, lot_size=None):
    """Send a trade signal via JSON file"""
    
    # Build signal string
    signal = side.upper()
    if sl_pips and tp_pips and lot_size:
        signal = f"{side.upper()} SL={sl_pips} TP={tp_pips} LOT={lot_size}"
    
    signal_data = {
        "signal": signal,
        "timestamp": datetime.now().isoformat() + "Z"
    }
    
    # Create unique filename (timestamp-based)
    filename = SIGNALS_FOLDER / f"signal_{datetime.now().strftime('%Y%m%d_%H%M%S%f')}.json"
    
    with open(filename, 'w') as f:
        json.dump(signal_data, f, indent=2)
    
    print(f"✅ Signal sent: {filename}")
    return filename

# Usage examples
if __name__ == "__main__":
    # Simple BUY signal
    send_signal("BUY")
    
    time.sleep(2)
    
    # SELL with specific parameters
    send_signal("SELL", sl_pips=75, tp_pips=150, lot_size=0.05)
```

### Example 2: C# Signal Generator

```csharp
using System;
using System.IO;
using System.Text.Json;
using System.Text.Json.Serialization;

class TradeSignal
{
    [JsonPropertyName("signal")]
    public string Signal { get; set; }
    
    [JsonPropertyName("timestamp")]
    public string Timestamp { get; set; }
}

class Program
{
    static void Main()
    {
        string signalsFolder = @"C:\Trades\signals";
        
        var signal = new TradeSignal
        {
            Signal = "BUY SL=50 TP=100 LOT=0.01",
            Timestamp = DateTime.UtcNow.ToString("O")
        };
        
        string filename = Path.Combine(
            signalsFolder,
            $"signal_{DateTime.Now:yyyyMMdd_HHmmss_fff}.json"
        );
        
        string json = JsonSerializer.Serialize(signal, new JsonSerializerOptions { WriteIndented = true });
        File.WriteAllText(filename, json);
        
        Console.WriteLine($"Signal written: {filename}");
    }
}
```

### Example 3: Manual Testing

Simply create a file `C:\Trades\signals\test_signal.json`:

```json
{
  "signal": "BUY SL=50 TP=100 LOT=0.01",
  "timestamp": "2025-09-16T14:30:45Z"
}
```

The bridge will detect it within 2 seconds and log it.

---

## Hybrid Mode (File + HTTP)

The bridge supports **both** file I/O and HTTP POST simultaneously:

### HTTP POST (for webhooks, REST clients)

```bash
curl -X POST http://localhost:8080/signal \
  -H "Content-Type: application/json" \
  -d '{"signal": "BUY SL=50 TP=100 LOT=0.01"}'
```

### File I/O (for local processes)

Just drop a JSON file into `C:\Trades\signals\`

**Both methods share the same signal queue**, so they don't interfere with each other.

---

## Troubleshooting

### Files Not Being Processed

**Problem:** JSON files sit in the folder without being deleted.

**Solutions:**
1. Check TradingBridge GUI for error messages
2. Verify JSON is valid: use an online JSON validator
3. Ensure `signal` field is not empty
4. Check file permissions on the signals folder

### Signals Queued/Rejected

**Problem:** Log shows `⚠️ QUEUED: '...' - waiting for previous signal`

**Solution:** This is normal! The bridge processes signals sequentially to prevent duplicate trades. The previous signal is still being executed by MT5 (or waiting for MT5 to complete). Wait 1-2 seconds and try again.

### Folder Not Found

**Problem:** `C:\Trades\signals\` doesn't exist

**Solution:** The bridge auto-creates it on startup. If it doesn't:
1. Create the folder manually
2. Check Windows permissions (need write access)
3. Restart TradingBridge.exe

### Invalid JSON Error

**Problem:** Log shows `❌ INVALID JSON in signal_xxx.json`

**Solution:**
- Double-check JSON syntax (commas, quotes, braces)
- Use proper string escaping: `"signal": "BUY SL=50"`
- Test with: `python -m json.tool signal_xxx.json`

---

## Performance Notes

- **Folder check interval:** 2 seconds (configurable in code)
- **MT5 polling interval:** 750ms (from EA)
- **Total latency:** ~2.75 seconds end-to-end
- **Max files per folder:** No limit (processes in order of creation)

For higher frequency trading, reduce `CHECK_INTERVAL` in `bridge.py`:
```python
CHECK_INTERVAL = 0.5  # 500ms instead of 2 seconds
```

---

## Migration from Chrome Extension to File I/O

If you're switching from the TradingView Chrome extension:

1. **Stop** using the extension (optional — you can keep both running)
2. **Update** bridge.py with the new file I/O code
3. **Restart** TradingBridge.exe
4. **Update** your signal source to write JSON files instead of POSTing
5. **Verify** signals appear in the TradingBridge log

No MT5 EA changes needed — the `/signal` endpoint remains the same.

---

## API Reference

### GET /signal

**Returns current signal state:**

```json
{
  "signal": "BUY SL=50 TP=100 LOT=0.01",
  "timestamp": "2025-09-16T14:30:45Z",
  "status": "NEW",
  "source": "file"
}
```

**Status values:**
- `"NEW"` — Signal received, waiting for MT5 to execute
- `"PROCESSED"` — Signal executed, ready for next one

**Source values:**
- `"file"` — Signal came from JSON file
- `"http"` — Signal came from HTTP POST
- `"none"` — No signal currently

---

### POST /signal

**Send signal via HTTP:**

```bash
curl -X POST http://localhost:8080/signal \
  -H "Content-Type: application/json" \
  -d '{"signal": "SELL SL=75 TP=150 LOT=0.02"}'
```

---

### POST /signal/processed

**Called by MT5 EA after trade execution:**

```bash
curl -X POST http://localhost:8080/signal/processed
```

---

## Advanced: Custom Folder Watcher

If you need sub-second latency or more control, you can replace the file watcher:

```python
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler

class SignalFileHandler(FileSystemEventHandler):
    def on_created(self, event):
        if not event.is_directory and event.src_path.endswith('.json'):
            process_signal_file(event.src_path)

observer = Observer()
observer.schedule(SignalFileHandler(), SIGNALS_FOLDER)
observer.start()
```

Install watchdog:
```bash
pip install watchdog
```

This achieves sub-100ms latency using file system events instead of polling.

---

## See Also

- [README.md](README.md) — Main documentation
- [Trading_Bot.mq5](Trading_Bot/Trading_Bot.mq5) — MT5 Expert Advisor
- [bridge.py](Trading_Bridge_Source/bridge.py) — Bridge server source code
