from datetime import datetime
import json
import os
from pathlib import Path
import threading
import time
import tkinter as tk
from tkinter import scrolledtext

from flask import Flask, jsonify, request
from flask_cors import CORS

app = Flask(__name__)
CORS(app)

SIGNALS_FOLDER = Path(os.environ.get("TRADING_BRIDGE_SIGNALS_FOLDER", r"C:\Trades\signals"))
CHECK_INTERVAL = float(os.environ.get("TRADING_BRIDGE_CHECK_INTERVAL", "2"))

current_signal = {
    "signal": "NONE",
    "timestamp": "",
    "status": "PROCESSED",
    "source": "none",
}

signal_lock = threading.Lock()
log_widget = None


def log(message):
    timestamp = datetime.now().strftime("%H:%M:%S")
    formatted = f"[{timestamp}] {message}"
    print(formatted)
    if log_widget is not None:
        log_widget.after(0, _append_log, formatted)


def _append_log(message):
    if log_widget is not None:
        log_widget.insert(tk.END, message + "\n")
        log_widget.see(tk.END)


def empty_signal():
    return {"signal": "NONE", "timestamp": "", "status": "PROCESSED", "source": "none"}


def clear_signal_manual():
    global current_signal
    with signal_lock:
        current_signal = empty_signal()
    log("🗑️ Signal manually cleared")


def process_signal_file(signal_file):
    global current_signal

    try:
        with signal_file.open("r", encoding="utf-8") as file_handle:
            data = json.load(file_handle)

        signal_value = str(data.get("signal", "")).strip()
        if not signal_value or signal_value.upper() == "NONE":
            log(f"⚠️ SKIPPED: {signal_file.name} - missing or empty signal")
            signal_file.unlink(missing_ok=True)
            return

        with signal_lock:
            if current_signal["status"] == "NEW":
                return
            current_signal = {
                "signal": signal_value,
                "timestamp": data.get("timestamp") or datetime.now().isoformat(),
                "status": "NEW",
                "source": "file",
                "source_file": signal_file.name,
            }

        log(f"📊 NEW file signal from '{signal_file.name}': {signal_value}")
        signal_file.unlink(missing_ok=True)
        log(f"✅ Processed and deleted {signal_file.name}")
    except json.JSONDecodeError as error:
        log(f"❌ INVALID JSON in {signal_file.name}: {error}")
        signal_file.unlink(missing_ok=True)
    except (OSError, PermissionError) as error:
        log(f"⚠️ Could not process {signal_file.name}: {error}")
    except Exception as error:
        log(f"❌ Error reading {signal_file.name}: {error}")


def watch_signals_folder():
    SIGNALS_FOLDER.mkdir(parents=True, exist_ok=True)
    log(f"👀 Watching folder: {SIGNALS_FOLDER}")
    while True:
        try:
            files = sorted(
                SIGNALS_FOLDER.glob("*.json"),
                key=lambda path: path.stat().st_mtime,
            )
            for signal_file in files:
                with signal_lock:
                    pending = current_signal["status"] == "NEW"
                if pending:
                    break
                process_signal_file(signal_file)
        except OSError as error:
            log(f"⚠️ Folder watcher error: {error}")
        time.sleep(CHECK_INTERVAL)


@app.route("/signal", methods=["GET"])
def get_signal():
    with signal_lock:
        return jsonify(current_signal)


@app.route("/signal", methods=["POST"])
def post_signal():
    global current_signal
    data = request.get_json(silent=True) or {}
    signal_value = str(data.get("signal", "")).strip()
    if not signal_value:
        return jsonify({"status": "rejected", "reason": "missing_signal"}), 400

    with signal_lock:
        if current_signal["status"] == "NEW":
            log(f"⚠️ REJECTED: '{signal_value}' - previous signal is pending")
            return jsonify({"status": "rejected", "reason": "signal_pending"}), 409
        current_signal = {
            "signal": signal_value,
            "timestamp": data.get("timestamp") or datetime.now().isoformat(),
            "status": "NEW",
            "source": "http",
        }
    log(f"📊 NEW HTTP signal: {signal_value}")
    return jsonify({"status": "success"})


@app.route("/signal/processed", methods=["POST"])
def mark_processed():
    with signal_lock:
        current_signal["status"] = "PROCESSED"
    log("✅ Signal marked as PROCESSED")
    return jsonify({"status": "success"})


@app.route("/signal/clear", methods=["POST"])
def clear_signal():
    clear_signal_manual()
    return jsonify({"status": "success"})


@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "running"})


def run_server():
    try:
        from waitress import serve
        log("🚀 Starting Waitress on http://127.0.0.1:8080")
        serve(app, host="127.0.0.1", port=8080, threads=4)
    except ImportError:
        log("⚠️ Waitress is not installed; using Flask development server")
        app.run(host="127.0.0.1", port=8080, debug=False, use_reloader=False)


def create_gui():
    global log_widget
    root = tk.Tk()
    root.title("TradingBridge Server - File I/O Mode")
    root.geometry("600x480")

    tk.Label(root, text="🚀 TradingBridge - File I/O Mode", font=("Arial", 14, "bold"), bg="#4CAF50", fg="white", pady=10).pack(fill=tk.X)
    tk.Label(root, text="✅ Server: http://127.0.0.1:8080", fg="green").pack(pady=5)
    tk.Label(root, text=f"📁 Watching: {SIGNALS_FOLDER}", fg="blue", wraplength=560).pack(pady=2)
    tk.Button(root, text="🗑️ Clear Old Signal", command=clear_signal_manual, bg="#FF6B6B", fg="white", padx=20, pady=5).pack(pady=5)
    tk.Label(root, text="Drop completed .json files into the watched folder.", fg="gray").pack()

    tk.Label(root, text="Signal Log:", font=("Arial", 9, "bold")).pack(pady=5)
    log_widget = scrolledtext.ScrolledText(root, height=18, font=("Courier", 9), bg="#1e1e1e", fg="#00ff00")
    log_widget.pack(padx=10, pady=5, fill=tk.BOTH, expand=True)

    SIGNALS_FOLDER.mkdir(parents=True, exist_ok=True)
    log("✅ Server started")
    log("📡 Waiting for JSON files or HTTP POST requests")
    threading.Thread(target=watch_signals_folder, daemon=True).start()
    threading.Thread(target=run_server, daemon=True).start()
    root.mainloop()


if __name__ == "__main__":
    print("WARNING: This software can execute live trades. Use at your own risk.")
    create_gui()
