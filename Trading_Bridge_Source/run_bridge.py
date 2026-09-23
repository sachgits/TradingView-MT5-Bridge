"""Entry point: start the bridge server, the inbox watcher, and the desktop panel.

Run directly (``python run_bridge.py``) or as the packaged ``Trading_Bridge.exe``.

    python run_bridge.py              # server + desktop panel
    python run_bridge.py --no-gui     # headless, e.g. as a service
    python run_bridge.py --port 8081

Configuration comes from ``TRADING_BRIDGE_*`` environment variables; the subset
that is safe to change while trading is also editable from the signal board and
persists across restarts.
"""

from __future__ import annotations

import argparse
import os
import sys
import threading
from pathlib import Path
from typing import Any, Optional

from bridge import contract
from bridge.config import DEFAULT_DATA_FOLDERNAME, DEFAULT_DB_FILENAME, Settings
from bridge.events import EventBus
from bridge.service import Service
from bridge.store import Store
from bridge.watcher import FolderWatcher
from bridge.web import create_app

# Each open SSE stream holds one Waitress worker thread for as long as a browser
# tab is open, so the pool has to be larger than the stream cap or the EA's poll
# would be queued behind dashboards.
SERVER_THREADS = 8
MAX_SSE_CLIENTS = 4


def build(settings: Optional[Settings] = None) -> dict:
    """Wire the components together. Returns them so a caller can start/stop."""
    settings = settings or Settings()
    conflict = settings.path_conflict()
    if conflict:
        raise ValueError(conflict)
    settings.ensure_dirs()
    store = Store(settings.db_path)
    bus = EventBus(max_clients=MAX_SSE_CLIENTS)
    service = Service(settings, store, bus)
    watcher = FolderWatcher(
        service, settings.signals_folder, settings.intake_dir, settings.interrupted_dir
    )
    service.attach_watcher(watcher)
    app = create_app(service)
    return {"settings": settings, "store": store, "bus": bus,
            "service": service, "watcher": watcher, "app": app}


def serve(app: Any, host: str, port: int, failure: list) -> None:
    try:
        from waitress import serve as waitress_serve
    except ImportError:
        # Flask's development server is single-threaded and would serialize the
        # EA's poll behind an open dashboard stream. It works, but say so.
        print("waitress is not installed; falling back to the Flask development server", file=sys.stderr)
        try:
            app.run(host=host, port=port, debug=False, use_reloader=False, threaded=True)
        except OSError as exc:
            failure.append(exc)
        return
    try:
        waitress_serve(app, host=host, port=port, threads=SERVER_THREADS, channel_timeout=120)
    except OSError as exc:
        failure.append(exc)


def main(argv: Optional[list] = None) -> int:
    parser = argparse.ArgumentParser(description="TradingView to MT5 bridge")
    parser.add_argument("--no-gui", action="store_true", help="run headless, without the desktop panel")
    parser.add_argument("--host", default=None, help="address to bind (default 127.0.0.1)")
    parser.add_argument("--port", type=int, default=None, help="port to bind (default 8080)")
    parser.add_argument("--folder", default=None,
                        help="inbox folder to read .json commands from (treated as read-only)")
    parser.add_argument("--data", default=None,
                        help="bridge data folder for the ledger and intake copies")
    args = parser.parse_args(argv)

    settings = Settings()
    if os.environ.get("TRADING_BRIDGE_SIGNALS_FOLDER"):
        settings.pinned.add("signals_folder")
    if args.host:
        settings.host = args.host
    if args.port:
        settings.port = int(args.port)
    if args.folder:
        settings.signals_folder = Path(args.folder)
        # An explicit flag outranks whatever the board saved last time.
        settings.pinned.add("signals_folder")
        # The data folder follows the inbox unless the operator pinned it, so a
        # --folder override does not silently leave the ledger in the old place.
        if not args.data and not os.environ.get("TRADING_BRIDGE_DATA_FOLDER"):
            settings.data_folder = settings.signals_folder.parent / DEFAULT_DATA_FOLDERNAME
    if args.data:
        settings.data_folder = Path(args.data)
    if (args.folder or args.data) and not os.environ.get("TRADING_BRIDGE_DB_PATH"):
        settings.db_path = settings.data_folder / DEFAULT_DB_FILENAME
    settings.clamp()

    conflict = settings.path_conflict()
    if conflict:
        print(f"refusing to start: {conflict}", file=sys.stderr)
        return 2

    try:
        parts = build(settings)
    except OSError as exc:
        print(f"cannot prepare {settings.data_folder}: {exc}", file=sys.stderr)
        return 2
    service: Service = parts["service"]
    watcher: FolderWatcher = parts["watcher"]
    base_url = f"http://{settings.host}:{settings.port}"

    print("=" * 72)
    print("TradingBridge - this software can place REAL orders on your broker account.")
    print("Validate every change on a demo account before using it live.")
    print("=" * 72)
    print(f"  inbox (read) : {settings.signals_folder}   [never written to; changeable on the board]")
    print(f"  data folder  : {settings.data_folder}")
    print(f"  intake copies: {settings.intake_dir}")
    print(f"  ledger       : {settings.db_path}")
    print(f"  signal board : {base_url}/")
    print(f"  EA endpoint  : {base_url}/signal  (Bot_Trader ServerURL)")
    print(f"  EA ack       : {base_url}/signal/processed  (Bot_Trader ProcessedURL)")
    print(f"  validation   : {settings.validation_mode}")
    print(f"  EA contract  : v{contract.EA_VERSION}, schema v{contract.SCHEMA_VERSION}")
    print()

    watcher.start()
    failure: list = []
    server = threading.Thread(target=serve, args=(parts["app"], settings.host, settings.port, failure),
                              name="bridge-server", daemon=True)
    server.start()

    gui = None
    if not args.no_gui:
        try:
            from bridge.gui import Gui

            gui = Gui(service, base_url)
        except ImportError as exc:
            print(f"desktop panel unavailable ({exc}); running headless", file=sys.stderr)

    exit_code = 0
    try:
        if gui is not None:
            gui.run()
        else:
            print("running headless; press Ctrl+C to stop")
            while server.is_alive():
                server.join(timeout=0.5)
            if failure:
                print(f"server stopped: {failure[0]}", file=sys.stderr)
                exit_code = 1
    except KeyboardInterrupt:
        print("\nstopping…")
    finally:
        watcher.stop()
        parts["store"].close()
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
