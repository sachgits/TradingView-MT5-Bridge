from __future__ import annotations

import json
import os
import shutil
import threading
import time
import tkinter as tk
from datetime import datetime, timezone
from pathlib import Path
from tkinter import scrolledtext
from typing import Any, Dict, Optional

from flask import Flask, jsonify, request
from flask_cors import CORS

from command_schema import normalize_command
from command_validator import validate_command

app = Flask(__name__)
CORS(app)

SIGNALS_FOLDER = Path(os.environ.get("TRADING_BRIDGE_SIGNALS_FOLDER", r"C:\Trades\signals"))
ARCHIVE_FOLDER = Path(os.environ.get("TRADING_BRIDGE_ARCHIVE_FOLDER", str(SIGNALS_FOLDER / "archive")))
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
    "source_file": None,
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
        "source_file": None,
        "error": None,
    }


def accept_command(payload: Any) -> Dict[str, Any]:
    normalized = normalize_command(payload)
    if "error" in normalized:
        raise ValueError(normalized["error"])
    validation = validate_command(normalized)
    if not validation["valid"]:
        raise ValueError("; ".join(validation["errors"]))
    return normalized


def process_payload(payload: Any, source: str, source_file: Optional[str] = None) -> Dict[str, Any]:
    normalized = accept_command(payload)
    normalized["timestamp"] = normalized.get("timestamp") or utc_now()
    normalized.setdefault("request_id", f"bridge-{int(time.time() * 1000)}")

    with signal_lock:
        global current_signal
        current_signal = {
            "signal": json.dumps(normalized, separators=(",", ":")),
            "normalized": normalized,
            "timestamp": normalized["timestamp"],
            "status": "NEW",
            "source": source,
            "request_id": normalized["request_id"],
            "source_file": source_file,
            "error": None,
        }
    log(f"📊 Accepted {source} command: {normalized.get('action')} {normalized.get('symbol', '')}")
    return current_signal


def archive_file(path: Path, status: str, error: Optional[str] = None) -> None:
    """Move the original JSON into an audit archive and append execution metadata."""
    ARCHIVE_FOLDER.mkdir(parents=True, exist_ok=True)
    try:
        with path.open("r", encoding="utf-8") as handle:
            original = json.load(handle)
    except Exception:
        original = {"_raw_file_read_failed": True}

    original["_bridge_audit"] = {
        "status": status,
        "received_at": original.get("timestamp") or utc_now(),
        "archived_at": utc_now(),
        "error": error,
    }
    destination = ARCHIVE_FOLDER / f"{path.stem}.{status.lower()}.{int(time.time() * 1000)}.json"
    temporary = destination.with_suffix(destination.suffix + ".tmp")
    try:
        with temporary.open("w", encoding="utf-8") as handle:
            json.dump(original, handle, indent=2)
        temporary.replace(destination)
        path.unlink(missing_ok=True)
        log(f"🗃️ Archived {path.name} as {destination.name}")
    except OSError as exc:
        log(f"⚠️ Could not archive {path.name}: {exc}")


def process_signal_file(path: Path) -> None:
    try:
        with path.open("r", encoding="utf-8") as handle:
            payload = json.load(handle)
        with signal_lock:
            if current_signal["status"] == "NEW":
                return
        accepted = process_payload(payload, "file", path.name)
        # The intake copy is archived immediately; MT5 receives the normalized command.
        archive_file(path, "ACCEPTED")
        log(f"✅ File accepted with request_id={accepted['request_id']}")
    except json.JSONDecodeError as exc:
        log(f"❌ Invalid JSON in {path.name}: {exc}")
        archive_file(path, "INVALID_JSON", str(exc))
    except ValueError as exc:
        log(f"❌ Rejected {path.name}: {exc}")
        archive_file(path, "REJECTED", str(exc))
    except OSError as exc:
        log(f"⚠️ File error for {path.name}: {exc}")


def watch_signals_folder() -> None:
    SIGNALS_FOLDER.mkdir(parents=True, exist_ok=True)
    ARCHIVE_FOLDER.mkdir(parents=True, exist_ok=True)
    log(f"👀 Watching folder: {SIGNALS_FOLDER}")
    log(f"🗃️ Audit archive: {ARCHIVE_FOLDER}")
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
        source_file = current_signal.get("source_file")
        request_id = current_signal.get("request_id")
        current_signal = empty_signal()
    log(f"✅ Command executed and acknowledged: request_id={request_id}, source_file={source_file}")
    return jsonify({"status": "success", "request_id": request_id, "source_file": source_file})


@app.route("/signal/clear", methods=["POST"])
def clear_signal():
    with signal_lock:
        global current_signal
        current_signal = empty_signal()
    log("🗑️ Pending command cleared; archived source files remain available")
    return jsonify({"status": "success"})


@app.route("/health", methods=["GET"])
def health():
    with signal_lock:
        status = current_signal["status"]
    return jsonify({"status": "running", "queue_status": status})


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
    root.geometry("680x520")
    tk.Label(root, text="🚀 TradingBridge", font=("Arial", 14, "bold"), bg="#4CAF50", fg="white", pady=10).pack(fill=tk.X)
    tk.Label(root, text="Local HTTP + JSON file intake · validated commands · audit archive", fg="green").pack(pady=5)
    tk.Label(root, text=f"📁 Input: {SIGNALS_FOLDER}\n🗃️ Archive: {ARCHIVE_FOLDER}", wraplength=630).pack(pady=2)
    tk.Button(root, text="🗑️ Clear pending command", command=clear_signal, bg="#FF6B6B", fg="white").pack(pady=8)
    log_widget = scrolledtext.ScrolledText(root, height=22, font=("Courier", 9), bg="#1e1e1e", fg="#00ff00")
    log_widget.pack(padx=10, pady=5, fill=tk.BOTH, expand=True)
    SIGNALS_FOLDER.mkdir(parents=True, exist_ok=True)
    ARCHIVE_FOLDER.mkdir(parents=True, exist_ok=True)
    threading.Thread(target=watch_signals_folder, daemon=True).start()
    threading.Thread(target=run_server, daemon=True).start()
    log("✅ Server started; input JSON is archived, never silently discarded")
    root.mainloop()


if __name__ == "__main__":
    print("WARNING: This software can execute live trades. Test on demo first.")
    create_gui()
