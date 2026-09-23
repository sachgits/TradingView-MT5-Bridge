"""HTTP surface: the EA's two endpoints, the REST API, and the signal board.

Three kinds of client talk to this server and they must not be confused:

* **Bot_Trader** polls ``GET /signal`` and acknowledges with
  ``POST /signal/processed``. These paths and payload shapes are fixed by the
  compiled EA - changing them silently breaks every installed bot - so they are
  served exactly as documented in the EA header.
* **Signal producers** (the Chrome extension, scripts, the read-only inbox) post
  commands to ``POST /signal`` or ``POST /api/v1/signals``.
* **The operator's browser** reads ``/api/v1/*`` and loads the board from ``/``.

The security split follows from that. MT5's ``WebRequest`` sends no ``Origin``
header and cannot send one, so origin checking applies only to the intake routes
a browser can reach. That check is not decorative: the server binds to loopback,
but loopback is not a trust boundary - any web page the operator visits can POST
to ``http://127.0.0.1:8080`` and, without it, place a trade.
"""

from __future__ import annotations

import json
import queue
import sys
from pathlib import Path
from typing import Any, Dict, Optional

from flask import Flask, Response, jsonify, request, send_from_directory, stream_with_context
from flask_cors import CORS

from . import contract
from .config import mutable_view
from .util import utc_now

SSE_KEEPALIVE_SECONDS = 15
SSE_RETRY_MS = 3000

# Describes the runtime-editable settings, bounds included, so the board can
# render its inputs from the contract instead of hardcoding them.
MUTABLE_VIEW = mutable_view()


def _web_root() -> Path:
    """Locate the dashboard assets, from source or from a PyInstaller bundle."""
    frozen = getattr(sys, "_MEIPASS", None)
    if frozen:
        return Path(frozen) / "web"
    return Path(__file__).resolve().parent.parent / "web"


def _int(value: Optional[str], default: int, lo: int, hi: int) -> int:
    try:
        number = int(str(value))
    except (TypeError, ValueError):
        return default
    return min(max(number, lo), hi)


def create_app(service: Any) -> Flask:
    """Build the Flask app around an existing Service."""
    app = Flask(__name__, static_folder=None)
    # Key order is part of the EA wire format: Bot_Trader finds the FIRST textual
    # occurrence of a key, so the envelope's own request_id/timestamp/status must
    # precede the nested command (see Service._build_envelope). Flask sorts keys
    # by default, which would put "normalized" first and let a command carrying
    # its own "request_id" or "status" shadow the envelope's. The config key
    # JSON_SORT_KEYS was removed in Flask 2.3; the provider attribute is current.
    app.json.sort_keys = False
    # The EA parses the body as text, so keep it compact and predictable.
    app.config["MAX_CONTENT_LENGTH"] = 1 * 1024 * 1024

    settings = service.settings
    CORS(
        app,
        origins=list(settings.allowed_origins),
        methods=["GET", "POST", "PATCH", "OPTIONS"],
        allow_headers=["Content-Type", "X-Bridge-Token"],
        expose_headers=["X-Bridge-Status"],
    )

    # ------------------------------------------------------------- guarding
    def origin_allowed() -> bool:
        if not settings.require_origin:
            return True
        origin = request.headers.get("Origin")
        # A non-browser client (curl, the extension's background page, a local
        # script) sends no Origin at all. Only a browser cross-origin request
        # carries one, and that is the case being defended against.
        if not origin:
            return True
        # The signal board is served by this very process, so its own origin is
        # first-party by definition. A page on another site cannot forge either
        # header: the browser sets Origin from the calling page and Host from
        # the URL, so equality of the two means same-origin.
        if origin.rstrip("/") == f"{request.scheme}://{request.host}":
            return True
        return origin.rstrip("/") in {o.rstrip("/") for o in settings.allowed_origins}

    def token_allowed() -> bool:
        if not settings.intake_token:
            return True
        supplied = request.headers.get("X-Bridge-Token", "")
        # Constant-time-ish comparison; the token is a local shared secret.
        return len(supplied) == len(settings.intake_token) and supplied == settings.intake_token

    def guard_intake() -> Optional[Response]:
        if not origin_allowed():
            return jsonify({
                "ok": False,
                "status": "error",
                "error": f"origin {request.headers.get('Origin')} is not allowed to submit trade commands",
            }), 403
        if not token_allowed():
            return jsonify({"ok": False, "status": "error", "error": "missing or wrong X-Bridge-Token"}), 401
        return None

    def read_body() -> Any:
        """Accept JSON, form text, or a bare legacy signal string."""
        payload = request.get_json(silent=True)
        if payload is not None:
            return payload
        text = (request.get_data(as_text=True) or "").strip()
        return text or None

    # ------------------------------------------------------- EA-facing routes
    @app.route("/signal", methods=["GET"])
    def ea_get_signal():
        return jsonify(service.ea_poll())

    @app.route("/signal/processed", methods=["POST"])
    def ea_ack():
        result = service.ea_ack(read_body())
        # The EA ignores the response body, but a 200 keeps it from retrying.
        return jsonify({"status": "success" if result.get("ok") else "error", **result})

    @app.route("/signal", methods=["POST"])
    def submit_signal():
        blocked = guard_intake()
        if blocked is not None:
            return blocked
        payload = read_body()
        if payload is None:
            return jsonify({"ok": False, "status": "error", "error": "a JSON body or legacy signal string is required"}), 400
        result = service.submit(payload, contract.SOURCE_HTTP)
        code = 200 if result["ok"] else (409 if result["status"] == "duplicate" else 422)
        return jsonify({"status": "success" if result["ok"] else "error", **result}), code

    @app.route("/signal/clear", methods=["POST"])
    def clear_signal():
        return jsonify(service.clear_queue("cleared via /signal/clear"))

    @app.route("/health", methods=["GET"])
    def health():
        state = service.ea_state()
        return jsonify({
            "status": "running",
            "queue_status": "PAUSED" if settings.paused else ("NEW" if service.store.queue_depth() else "PROCESSED"),
            "queue_depth": service.store.queue_depth(),
            "ea_online": state["online"],
            "ea_last_seen_at": state["last_seen_at"],
            "watcher": service.watcher.status() if service.watcher is not None else None,
            "version": contract.EA_VERSION,
            "schema_version": contract.SCHEMA_VERSION,
        })

    # ------------------------------------------------------------- API v1
    @app.route("/api/v1/state", methods=["GET"])
    def api_state():
        return jsonify(service.snapshot(
            limit=_int(request.args.get("limit"), 50, 1, 500),
            since_event=_int(request.args.get("since_event"), 0, 0, 10 ** 12),
            metrics_window=_int(request.args.get("window"), 3600, 60, 86_400 * 7),
        ))

    @app.route("/api/v1/signals", methods=["GET"])
    def api_list_signals():
        return jsonify({
            "signals": service.list_signals(
                limit=_int(request.args.get("limit"), 100, 1, 1000),
                offset=_int(request.args.get("offset"), 0, 0, 10 ** 9),
                status=request.args.get("status") or None,
                symbol=request.args.get("symbol") or None,
                action=request.args.get("action") or None,
                search=request.args.get("search") or None,
            ),
            "filters": {
                "symbols": service.store.distinct_values("symbol"),
                "actions": service.store.distinct_values("action"),
                "statuses": service.store.distinct_values("status"),
                "sources": service.store.distinct_values("source"),
            },
        })

    @app.route("/api/v1/signals", methods=["POST"])
    def api_submit():
        blocked = guard_intake()
        if blocked is not None:
            return blocked
        payload = read_body()
        if payload is None:
            return jsonify({"ok": False, "error": "a command body is required"}), 400
        # The board posts {"command": {...}} so a command that legitimately has
        # a "command" key is not mistaken for the wrapper.
        if isinstance(payload, dict) and set(payload) == {"command"}:
            payload = payload["command"]
        request_id = request.args.get("request_id") or None
        result = service.submit(payload, contract.SOURCE_API, request_id=request_id)
        code = 201 if result["ok"] else (409 if result["status"] == "duplicate" else 422)
        return jsonify(result), code

    @app.route("/api/v1/signals/<request_id>", methods=["GET"])
    def api_signal_detail(request_id: str):
        row = service.signal_detail(request_id)
        if row is None:
            return jsonify({"ok": False, "error": "unknown request_id"}), 404
        return jsonify(row)

    @app.route("/api/v1/signals/<request_id>/cancel", methods=["POST"])
    def api_cancel(request_id: str):
        result = service.cancel(request_id)
        return jsonify(result), (200 if result.get("ok") else 409)

    @app.route("/api/v1/signals/<request_id>/retry", methods=["POST"])
    def api_retry(request_id: str):
        result = service.retry(request_id)
        return jsonify(result), (201 if result.get("ok") else 409)

    @app.route("/api/v1/signals/<request_id>/requeue", methods=["POST"])
    def api_requeue(request_id: str):
        result = service.requeue(request_id)
        return jsonify(result), (200 if result.get("ok") else 409)

    @app.route("/api/v1/validate", methods=["POST"])
    def api_validate():
        payload = read_body()
        if payload is None:
            return jsonify({"valid": False, "errors": ["a command body is required"]}), 400
        if isinstance(payload, dict) and set(payload) == {"command"}:
            payload = payload["command"]
        return jsonify(service.preview(payload))

    @app.route("/api/v1/events", methods=["GET"])
    def api_events():
        return jsonify({
            "events": service.store.list_events(
                since_id=_int(request.args.get("since"), 0, 0, 10 ** 12),
                limit=_int(request.args.get("limit"), 200, 1, 1000),
                request_id=request.args.get("request_id") or None,
            )
        })

    @app.route("/api/v1/metrics", methods=["GET"])
    def api_metrics():
        return jsonify(service.store.metrics(_int(request.args.get("window"), 3600, 60, 86_400 * 7)))

    @app.route("/api/v1/settings", methods=["GET"])
    def api_get_settings():
        return jsonify({
            "settings": settings.public_dict(),
            "editable": MUTABLE_VIEW,
        })

    @app.route("/api/v1/settings", methods=["PATCH", "POST"])
    def api_patch_settings():
        updates = request.get_json(silent=True)
        if not isinstance(updates, dict):
            return jsonify({"ok": False, "errors": {"body": "a JSON object of settings is required"}}), 400
        blocked = guard_intake()
        if blocked is not None:
            return blocked
        public, errors = service.update_settings(updates)
        return jsonify({"ok": not errors, "settings": public, "errors": errors}), (200 if not errors else 422)

    @app.route("/api/v1/contract", methods=["GET"])
    def api_contract():
        return jsonify(contract.public_contract())

    @app.route("/api/v1/watcher", methods=["GET"])
    def api_watcher():
        if service.watcher is None:
            return jsonify({"running": False, "reason": "file intake is not running in this process"})
        return jsonify(service.watcher.status())

    @app.route("/api/v1/rescan", methods=["POST"])
    def api_rescan():
        if service.watcher is None:
            return jsonify({"ok": False, "error": "file intake is not running in this process"}), 409
        results = service.watcher.scan_once()
        return jsonify({"ok": True, "scanned": len(results), "results": results})

    # ------------------------------------------------------------------- SSE
    @app.route("/api/v1/stream", methods=["GET"])
    def api_stream():
        """Server-sent events, with a hard cap on concurrent streams.

        Waitress serves each open stream from its own worker thread, so an
        unbounded number of browser tabs would exhaust the pool and stop the EA
        from polling. When the cap is reached this answers 503 and the board
        falls back to polling /api/v1/events, which is why every event also
        lives in the ledger.
        """
        since = _int(request.args.get("since"), 0, 0, 10 ** 12)
        subscriber = service.bus.subscribe()
        if subscriber is None:
            response = jsonify({
                "ok": False,
                "error": "too many live streams; use /api/v1/events polling instead",
            })
            response.status_code = 503
            response.headers["Retry-After"] = "5"
            return response

        def encode(event: Dict[str, Any]) -> str:
            return f"id: {event.get('id', 0)}\nevent: {event.get('type', 'message')}\ndata: {json.dumps(event, default=str)}\n\n"

        def generate():
            try:
                yield f"retry: {SSE_RETRY_MS}\n\n"
                # Backfill before switching to live, so a client that reconnects
                # with its last id sees everything it missed.
                for event in service.store.list_events(since_id=since, limit=500):
                    yield encode(event)
                while True:
                    try:
                        event = subscriber.get(timeout=SSE_KEEPALIVE_SECONDS)
                    except queue.Empty:
                        # A named event, not a ": comment" line: EventSource
                        # never surfaces comments to script, so a comment-only
                        # keepalive looks identical to a dead stream from the
                        # board's stall watchdog. This frame is not in the
                        # ledger and carries no id, so it never moves a cursor.
                        yield f"event: heartbeat\ndata: {json.dumps({'type': 'heartbeat', 'at': utc_now()})}\n\n"
                        continue
                    yield encode(event)
            finally:
                service.bus.unsubscribe(subscriber)

        response = Response(stream_with_context(generate()), mimetype="text/event-stream")
        response.headers["Cache-Control"] = "no-cache, no-transform"
        # No "Connection: keep-alive" here: it is a hop-by-hop header, and PEP
        # 3333 forbids a WSGI app from setting one - Waitress aborts the request
        # with an AssertionError. Keep-alive is the server's own decision.
        response.headers["X-Accel-Buffering"] = "no"
        return response

    # ------------------------------------------------------------- dashboard
    @app.route("/", methods=["GET"])
    def dashboard():
        return send_from_directory(_web_root(), "index.html")

    @app.route("/<path:filename>", methods=["GET"])
    def dashboard_asset(filename: str):
        root = _web_root()
        candidate = (root / filename).resolve()
        # send_from_directory already refuses traversal, but this server sits in
        # front of a live trading account and the check is cheap.
        if root not in candidate.parents:
            return jsonify({"ok": False, "error": "not found"}), 404
        return send_from_directory(root, filename)

    @app.route("/favicon.ico", methods=["GET"])
    def favicon():
        return ("", 204)

    # ---------------------------------------------------------- error handlers
    @app.errorhandler(404)
    def not_found(_):
        return jsonify({"ok": False, "error": "not found"}), 404

    @app.errorhandler(405)
    def not_allowed(_):
        return jsonify({"ok": False, "error": "method not allowed"}), 405

    @app.errorhandler(413)
    def too_large(_):
        return jsonify({"ok": False, "error": "payload exceeds 1 MiB"}), 413

    @app.errorhandler(Exception)
    def unhandled(exc):
        # A stack trace in the log, a stable shape on the wire: the board and
        # the EA both need to keep working after an unexpected failure.
        app.logger.exception("unhandled error on %s %s", request.method, request.path)
        service.emit("server.error", None, {"path": request.path, "error": f"{type(exc).__name__}: {exc}"})
        return jsonify({"ok": False, "error": "internal bridge error", "detail": str(exc)}), 500

    return app
