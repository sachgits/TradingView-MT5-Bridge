from __future__ import annotations

import json
import os
import re
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional

from flask import Flask, jsonify, request
from flask_cors import CORS

from command_schema import normalize_command
from command_validator import validate_command

app = Flask(__name__)
CORS(app)

SIGNALS_FOLDER = Path(os.environ.get("TRADING_BRIDGE_SIGNALS_FOLDER", r"C:\Trades\signals"))
CHECK_INTERVAL = float(os.environ.get("TRADING_BRIDGE_CHECK_INTERVAL", "2"))

current_signal: Dict[str, Any] = {
    "signal": "NONE",
    "normalized": None,
    "timestamp": "",
    "status": "PROCESSED",
    "source": "none",
}

signal_lock = threading.Lock()
log_widget = None


def log(message: str) -> None:
    timestamp = datetime.now().strftime("%H:%M:%S")
    formatted = f"[{timestamp}] {message}"
    print(formatted)
    if log_widget is not None:
        log_widget.after(0, _append_log, formatted)


def _append_log(message: str) -> None:
    if log_widget is not None:
        log_widget.insert("end", message + "\n")
        log_widget.see("end")


def empty_signal() -> Dict[str, Any]:
    return {
        "signal": "NONE",
        "normalized": None,
        "timestamp": "",
        "status": "PROCESSED",
        "source": "none",
    }


def _read_legacy_signal(signal_text: str, extras: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    extras = extras or {}
    match = re.search(r"\b(BUY|SELL|LONG|SHORT)\b", str(signal_text), re.IGNORECASE)
    action = (match.group(1).lower() if match else "buy")
    if action in {"long"}:
        action = "buy"
    if action in {"short"}:
        action = "sell"

    def parse_value(key: str, default: float) -> float:
        m = re.search(rf"{re.escape(key)}\s*=\s*([-+]?\d+(?:\.\d+)?)", str(signal_text), re.IGNORECASE)
        if not m:
            return default
        try:
            return float(m.group(1))
        except ValueError:
            return default

    symbol = extras.get("symbol")
    size = extras.get("size")
    if isinstance(size, dict):
        size_value = float(size.get("value", 0.01))
    else:
        size_value = float(size or 0.01)

    normalized = {
        "schema_version": "1.0",
        "action": action,
        "symbol": symbol,
        "size": {"type": "fixed", "unit": "lots", "value": size_value},
        "stop_loss": {"type": "distance", "unit": "points", "value": parse_value("SL", 50.0)},
        "take_profit": {"type": "distance", "unit": "points", "value": parse_value("TP", 100.0)},
        "legacy_signal": str(signal_text),
    }
    lot_value = parse_value("LOT", size_value)
    normalized["size"] = {"type": "fixed", "unit": "lots", "value": lot_value}
    return normalized


def accept_command(payload: Any) -> Dict[str, Any]:
    if payload is None:
        raise ValueError("empty payload")

    if isinstance(payload, str):
        payload = payload.strip()
        if not payload:
            raise ValueError("empty payload")
        try:
            payload = json.loads(payload)
        except json.JSONDecodeError:
            normalized = _read_legacy_signal(payload)
            validation = validate_command(normalized)
            if not validation["valid"]:
                raise ValueError("; ".join(validation["errors"]))
            return normalized

    normalized = normalize_command(payload)
    if "error" in normalized:
        raise ValueError(normalized["error"])

    validation = validate_command(normalized)
    if not validation["valid"]:
        raise ValueError("; ".join(validation["errors"]))

    return normalized


def process_signal_payload(payload: Any, source: str = "http") -> Dict[str, Any]:
    global current_signal
    normalized = accept_command(payload)
    with signal_lock:
        current_signal = {
            "signal": json.dumps(normalized),
            "normalized": normalized,
            "timestamp": datetime.now().isoformat(),
            "status": "NEW",
            "source": source,
        }
    log(f"📊 NEW {source.upper()} command: {normalized}")
    return current_signal


def process_signal_file(signal_file: Path) -> None:
    try:
        with signal_file.open("r", encoding="utf-8") as handle:
            data = json.load(handle)
        process_signal_payload(data, source="file")
        signal_file.unlink(missing_ok=True)
        log(f"✅ Processed and deleted file: {signal_file.name}")
    except json.JSONDecodeError as exc:
        log(f"❌ INVALID JSON in {signal_file.name}: {exc}")
        signal_file.unlink(missing_ok=True)
    except Exception as exc:
        log(f"⚠️ Could not process {signal_file.name}: {exc}")


def watch_signals_folder() -> None:
    SIGNALS_FOLDER.mkdir(parents=True, exist_ok=True)
    log(f"👀 Watching folder: {SIGNALS_FOLDER}")
    while True:
        try:
            files = sorted(SIGNALS_FOLDER.glob("*.json"), key=lambda p: p.stat().st_mtime)
            for path in files:
                with signal_lock:
                    pending = current_signal.get("status") == "NEW"
                if pending:
                    break
                process_signal_file(path)
        except OSError as exc:
            log(f"⚠️ Folder watcher error: {exc}")
        time.sleep(CHECK_INTERVAL)


@app.route("/signal", methods=["GET"])
def get_signal():
    with signal_lock:
        return jsonify(current_signal)


@app.route("/signal", methods=["POST"])
def post_signal():
    global current_signal
    data = request.get_json(silent=True) or {}
    with signal_lock:
        if current_signal.get("status") == "NEW":
            return jsonify({"status": "rejected", "reason": "signal_pending"}), 409
    try:
        normalized_signal = process_signal_payload(data, source="http")
        return jsonify({"status": "success", "normalized": normalized_signal["normalized"]})
    except ValueError as exc:
        return jsonify({"status": "error", "error": str(exc)}), 400


@app.route("/signal/processed", methods=["POST"])
def mark_processed():
    global current_signal
    with signal_lock:
        current_signal["status"] = "PROCESSED"
        current_signal["signal"] = "NONE"
        current_signal["normalized"] = None
    log("✅ Signal marked as PROCESSED")
    return jsonify({"status": "success"})


@app.route("/signal/clear", methods=["POST"])
def clear_signal():
    global current_signal
    with signal_lock:
        current_signal = empty_signal()
    log("🗑️ Signal manually cleared")
    return jsonify({"status": "success"})


@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "running"})


def run_server() -> None:
    try:
        from waitress import serve
        log("🚀 Starting Waitress server on http://127.0.0.1:8080")
        serve(app, host="127.0.0.1", port=8080, threads=4)
    except ImportError:
        log("⚠️ Waitress not installed; using Flask dev server")
        app.run(host="127.0.0.1", port=8080, debug=False, use_reloader=False)


def create_gui() -> None:
    global log_widget
    root = tk.Tk() if False else None
    if root is not None:
        root.title("TradingBridge Server")
        root.geometry("620x460")
        tk.Label(root, text="🚀 TradingBridge", font=("Arial", 14, "bold"), bg="#4CAF50", fg="white", pady=10).pack(fill=tk.X)
        tk.Label(root, text=f"✅ Server: http://127.0.0.1:8080", fg="green").pack(pady=5)
        tk.Label(root, text=f"📁 Watching: {SIGNALS_FOLDER}", wraplength=560).pack(pady=5)
        tk.Button(root, text="🗑️ Clear Old Signal", command=lambda: clear_signal(), bg="#FF6B6B", fg="white", padx=20, pady=5).pack(pady=8)
        log_widget = scrolledtext.ScrolledText(root, height=18, font=("Courier", 9), bg="#1e1e1e", fg="#00ff00")
        log_widget.pack(padx=10, pady=5, fill=tk.BOTH, expand=True)
        root.mainloop()


if __name__ == "__main__":
    SIGNALS_FOLDER.mkdir(parents=True, exist_ok=True)
    print("WARNING: This software can execute live trades. Use at your own risk.")
    threading.Thread(target=watch_signals_folder, daemon=True).start()
    threading.Thread(target=run_server, daemon=True).start()
    while True:
        time.sleep(1)






















































































































