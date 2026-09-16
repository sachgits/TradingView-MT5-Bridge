from flask import Flask, request, jsonify
from flask_cors import CORS
import tkinter as tk
from tkinter import scrolledtext
import threading
from datetime import datetime
import os
import json
from pathlib import Path
import time

app = Flask(__name__)
CORS(app)

# Configuration
SIGNALS_FOLDER = r"C:\Trades\signals"  # User configurable path
CHECK_INTERVAL = 2  # seconds between folder checks

# Store signal with status flag
current_signal = {
    "signal": "NONE",
    "timestamp": "",
    "status": "PROCESSED",
    "source": "none"  # "http", "file", or "none"
}

log_widget = None

def log(msg):
    """Add message to GUI"""
    if log_widget:
        time_str = datetime.now().strftime("%H:%M:%S")
        log_widget.insert(tk.END, f"[{time_str}] {msg}\n")
        log_widget.see(tk.END)
    print(msg)


def clear_signal_manual():
    """Manual clear button"""
    global current_signal
    current_signal = {
        "signal": "NONE",
        "timestamp": "",
        "status": "PROCESSED",
        "source": "none"
    }
    log(f"✅ Signal manually cleared!")


def watch_signals_folder():
    """Monitor folder for new JSON signal files
    
    Watches SIGNALS_FOLDER for *.json files containing trade signals.
    Files should contain:
    {
        "signal": "BUY SL=50 TP=100 LOT=0.01",
        "timestamp": "2025-09-16T14:30:45Z"
    }
    
    Files are processed in order of creation and deleted after processing.
    """
    global current_signal
    
    processed_files = set()
    
    while True:
        try:
            # Ensure folder exists
            if not os.path.exists(SIGNALS_FOLDER):
                time.sleep(CHECK_INTERVAL)
                continue
            
            # Find all JSON files in folder, sorted by creation time (oldest first)
            json_files = sorted(
                Path(SIGNALS_FOLDER).glob("*.json"),
                key=lambda f: f.stat().st_ctime
            )
            
            for json_file in json_files:
                file_key = str(json_file)
                
                # Skip already processed files
                if file_key in processed_files:
                    continue
                
                try:
                    # Read JSON file
                    with open(json_file, 'r') as f:
                        data = json.load(f)
                    
                    # Extract signal from JSON
                    signal_value = data.get('signal', 'NONE')
                    
                    # Validate signal
                    if not signal_value or signal_value == 'NONE':
                        log(f"⚠️ SKIPPED: {json_file.name} - invalid or empty signal")
                        processed_files.add(file_key)
                        try:
                            os.remove(json_file)
                        except:
                            pass
                        continue
                    
                    # Check if previous signal is still pending
                    if current_signal["status"] == "NEW":
                        log(f"⚠️ QUEUED: '{json_file.name}' - waiting for previous signal to complete")
                        # Don't mark as processed yet; retry next iteration
                        continue
                    
                    # Set new signal from file
                    current_signal = {
                        "signal": signal_value,
                        "timestamp": datetime.now().isoformat(),
                        "status": "NEW",
                        "source": "file",
                        "source_file": json_file.name
                    }
                    
                    log(f"📊 NEW Signal from '{json_file.name}': {signal_value}")
                    
                    # Mark as processed
                    processed_files.add(file_key)
                    
                    # Delete file after successful processing
                    try:
                        os.remove(json_file)
                        log(f"✅ Processed and deleted {json_file.name}")
                    except Exception as e:
                        log(f"⚠️ Could not delete {json_file.name}: {e}")
                    
                except json.JSONDecodeError as e:
                    log(f"❌ INVALID JSON in {json_file.name}: {e}")
                    processed_files.add(file_key)
                    try:
                        os.remove(json_file)
                    except:
                        pass
                except Exception as e:
                    log(f"❌ ERROR reading {json_file.name}: {e}")
                
        except Exception as e:
            log(f"❌ Watcher error: {e}")
        
        time.sleep(CHECK_INTERVAL)


@app.route('/signal', methods=['GET'])
def get_signal():
    """MT5 reads from here
    
    Returns current signal in JSON format:
    {
        "signal": "BUY SL=50 TP=100 LOT=0.01",
        "timestamp": "2025-09-16T14:30:45Z",
        "status": "NEW" or "PROCESSED",
        "source": "http" or "file"
    }
    """
    return jsonify(current_signal)


@app.route('/signal', methods=['POST'])
def post_signal():
    """Accept signals via HTTP POST (backward compatible)
    
    Request body:
    {
        "signal": "SELL"
    }
    """
    global current_signal
    data = request.json
    signal_value = data.get('signal', 'NONE')
    
    # Check if signal is already pending
    if current_signal["status"] == "NEW":
        log(f"⚠️ REJECTED: '{signal_value}' - Previous signal still pending!")
        return jsonify({"status": "rejected", "reason": "signal_pending"})
    
    # Set new signal from HTTP
    current_signal = {
        "signal": signal_value,
        "timestamp": datetime.now().isoformat(),
        "status": "NEW",
        "source": "http"
    }
    
    log(f"📊 NEW Signal (HTTP): {signal_value}")
    return jsonify({"status": "success"})


@app.route('/signal/processed', methods=['POST'])
def mark_processed():
    """MT5 calls this after executing trade"""
    global current_signal
    current_signal["status"] = "PROCESSED"
    log(f"✅ Signal marked as PROCESSED")
    return jsonify({"status": "success"})


@app.route('/signal/clear', methods=['POST'])
def clear_signal():
    """Reset to NONE"""
    global current_signal
    current_signal = {
        "signal": "NONE",
        "timestamp": "",
        "status": "PROCESSED",
        "source": "none"
    }
    log("🗑️ Signal cleared")
    return jsonify({"status": "success"})


@app.route('/health', methods=['GET'])
def health():
    """Health check endpoint"""
    return jsonify({"status": "running"})


def run_production_server():
    """Run production server with Waitress"""
    try:
        # Try to import waitress (production server)
        from waitress import serve
        log("🚀 Starting production server (Waitress)...")
        serve(app, host='127.0.0.1', port=8080, threads=4)
    except ImportError:
        # Fallback to Flask dev server if waitress not installed
        log("⚠️ Waitress not found, using development server")
        log("⚠️ For production use: pip install waitress")
        app.run(host='127.0.0.1', port=8080, debug=False, use_reloader=False)


def create_gui():
    global log_widget
    
    root = tk.Tk()
    root.title("TradingBridge Server - File I/O Mode")
    root.geometry("600x480")
    
    # Header
    header = tk.Label(root, text="🚀 TradingView Bridge - File I/O Mode", 
                      font=("Arial", 14, "bold"), 
                      bg="#4CAF50", fg="white", pady=10)
    header.pack(fill=tk.X)
    
    # Status
    status = tk.Label(root, text="✅ Production Server Running on localhost:8080", 
                      font=("Arial", 10), fg="green")
    status.pack(pady=5)
    
    # Folder info
    folder_info = tk.Label(root, text=f"📁 Watching: {SIGNALS_FOLDER}", 
                          font=("Arial", 9), fg="blue", wraplength=550)
    folder_info.pack(pady=2)
    
    # Mode indicator
    mode_label = tk.Label(root, text="Mode: File I/O (+ HTTP fallback)", 
                         font=("Arial", 9), fg="orange")
    mode_label.pack(pady=2)
    
    # Clear button
    clear_btn = tk.Button(root, 
                         text="🗑️ Clear Old Signal", 
                         command=clear_signal_manual,
                         font=("Arial", 10, "bold"),
                         bg="#FF6B6B", 
                         fg="white",
                         cursor="hand2",
                         relief="raised",
                         padx=20,
                         pady=5)
    clear_btn.pack(pady=5)
    
    # Instructions
    info = tk.Label(root, text="Drop JSON files into the signals folder or POST to /signal", 
                    font=("Arial", 9), fg="gray")
    info.pack()
    
    # Log section
    tk.Label(root, text="Signal Log:", font=("Arial", 9, "bold")).pack(pady=5)
    
    log_widget = scrolledtext.ScrolledText(root, height=16, 
                                           font=("Courier", 9),
                                           bg="#1e1e1e", fg="#00ff00")
    log_widget.pack(padx=10, pady=5, fill=tk.BOTH, expand=True)
    
    # Initial messages
    log("✅ Server started successfully!")
    log("🔧 Production mode - No Flask warnings!")
    log(f"👀 Watching folder: {SIGNALS_FOLDER}")
    log("📡 Waiting for JSON signal files or HTTP POST requests...")
    log("")
    log("JSON file format:")
    log('  {"signal": "BUY SL=50 TP=100 LOT=0.01"}')
    
    # Ensure signals folder exists
    try:
        os.makedirs(SIGNALS_FOLDER, exist_ok=True)
        log(f"✅ Signals folder ready: {SIGNALS_FOLDER}")
    except Exception as e:
        log(f"❌ Could not create signals folder: {e}")
    
    # Start file watcher in background thread
    watcher_thread = threading.Thread(target=watch_signals_folder, daemon=True)
    watcher_thread.start()
    
    # Start server in background thread
    server_thread = threading.Thread(target=run_production_server, daemon=True)
    server_thread.start()
    
    root.mainloop()


if __name__ == '__main__':
    print("WARNING: This software can execute live trades. Use at your own risk. See README for full disclaimer.")
    create_gui()
