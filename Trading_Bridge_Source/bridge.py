from __future__ import annotations

import json
import os
import threading
import time
import tkinter as tk
from datetime import datetime, timezone
from pathlib import Path
from tkinter import scrolledtext
from typing import Any, Dict

from flask import Flask, jsonify, request
from flask_cors import CORS

from command_schema import normalize_command
from command_validator import validate_command

app = Flask(__name__)
CORS(app)

SIGNALS_FOLDER = Path(os.environ.get("TRADING_BRIDGE_SIGNALS_FOLDER", r"C:\Trades\signals"))
CHECK_INTERVAL = float(os.environ.get("TRADING_BRIDGE_CHECK_INTERVAL", "2"))

signal_lock = threading.Lock()
log_widget = None
current_signal: Dict[str, Any] = {
    "signal": "NONE",
    "normalized": None,
    "timestamp": "",
    "status": "PROCESSED",
    "source": "none",
    "request_id": None,
    "error": None,
}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def log(message: str) -> None:
    formatted = f"[{datetime.now().strftime('%H:%M:%S')}] {message}"
    print(formatted)
    if log_widget is not None:
        log_widget.after(0, _append_log, formatted)


def _append_log(message: str) -> None:
    if log_widget is not None:
        log_widget.insert(tk.END, message + "\n")
        log_widget.see(tk.END)


def empty_signal() -> Dict[str, Any]:
    return {
        "signal": "NONE",
        "normalized": None,
        "timestamp": "",
        "status": "PROCESSED",
        "source": "none",
        "request_id": None,
        "error": None,
    }


def accept_command(payload: Any) -> Dict[str, Any]:
    normalized = normalize_command(payload)
    if "error" in normalized:
        raise ValueError(normalized["error"])
    result = validate_command(normalized)
    if not result["valid"]:
        raise ValueError("; ".join(result["errors"]))
    return normalized


def process_payload(payload: Any, source: str) -> Dict[str, Any]:
    normalized = accept_command(payload)
    timestamp = normalized.get("timestamp") or utc_now()
    normalized["timestamp"] = timestamp
    with signal_lock:
        global current_signal
        current_signal = {
            "signal": json.dumps(normalized, separators=(",", ":")),
            "normalized": normalized,
            "timestamp": timestamp,
            "status": "NEW",
            "source": source,
            "request_id": normalized.get("request_id"),
            "error": None,
        }
    log(f"📊 Accepted {source} command: {normalized.get('action')} {normalized.get('symbol', '')}")
    return current_signal


def process_signal_file(path: Path) -> None:
    try:
        with path.open("r", encoding="utf-8") as handle:
            payload = json.load(handle)
        with signal_lock:
            pending = current_signal["status"] == "NEW"
        if pending:
            return
        process_payload(payload, "file")
        path.unlink(missing_ok=True)
    except json.JSONDecodeError as exc:
        log(f"❌ Invalid JSON in {path.name}: {exc}")
        path.unlink(missing_ok=True)
    except ValueError as exc:
        log(f"❌ Rejected {path.name}: {exc}")
        path.unlink(missing_ok=True)
    except OSError as exc:
        log(f"⚠️ File error for {path.name}: {exc}")


def watch_signals_folder() -> None:
    SIGNALS_FOLDER.mkdir(parents=True, exist_ok=True)
    log(f"👀 Watching folder: {SIGNALS_FOLDER}")
    while True:
        try:
            for path in sorted(SIGNALS_FOLDER.glob("*.json"), key=lambda item: item.stat().st_mtime):
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
    with signal_lock:
        if current_signal["status"] == "NEW":
            return jsonify({"status": "rejected", "reason": "signal_pending"}), 409
    payload = request.get_json(silent=True)
    if payload is None:
        return jsonify({"status": "error", "error": "valid JSON object required"}), 400
    try:
        accepted = process_payload(payload, "http")
        return jsonify({"status": "success", "normalized": accepted["normalized"]})
    except ValueError as exc:
        return jsonify({"status": "error", "error": str(exc)}), 400


@app.route("/signal/processed", methods=["POST"])
def mark_processed():
    with signal_lock:
        global current_signal
        current_signal = empty_signal()
    log("✅ Command acknowledged as PROCESSED")
    return jsonify({"status": "success"})


@app.route("/signal/clear", methods=["POST"])
def clear_signal():
    with signal_lock:
        global current_signal
        current_signal = empty_signal()
    log("🗑️ Command queue cleared")
    return jsonify({"status": "success"})


@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "running", "queue_status": current_signal["status"]})


def run_server() -> None:
    try:
        from waitress import serve
        serve(app, host="127.0.0.1", port=8080, threads=4)
    except ImportError:
        app.run(host="127.0.0.1", port=8080, debug=False, use_reloader=False)


def create_gui() -> None:
    global log_widget
    root = tk.Tk()
    root.title("TradingBridge - Universal Command Mode")
    root.geometry("650x500")
    tk.Label(root, text="🚀 TradingBridge", font=("Arial", 14, "bold"), bg="#4CAF50", fg="white", pady=10).pack(fill=tk.X)
    tk.Label(root, text="Local HTTP + JSON file intake · validation before MT5", fg="green").pack(pady=5)
    tk.Label(root, text=f"📁 Watching: {SIGNALS_FOLDER}", wraplength=600).pack(pady=2)
    tk.Button(root, text="🗑️ Clear pending command", command=clear_signal, bg="#FF6B6B", fg="white").pack(pady=8)
    log_widget = scrolledtext.ScrolledText(root, height=20, font=("Courier", 9), bg="#1e1e1e", fg="#00ff00")
    log_widget.pack(padx=10, pady=5, fill=tk.BOTH, expand=True)
    SIGNALS_FOLDER.mkdir(parents=True, exist_ok=True)
    threading.Thread(target=watch_signals_folder, daemon=True).start()
    threading.Thread(target=run_server, daemon=True).start()
    log("✅ Server started; incomplete commands are rejected, not guessed")
    root.mainloop()


if __name__ == "__main__":
    print("WARNING: This software can execute live trades. Test on demo first.")
    create_gui()
