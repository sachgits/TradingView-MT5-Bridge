"""Desktop panel for the bridge.

The web signal board is the full interface; this window exists so the operator
has something to look at when the browser is closed, and so the process has a
way to be stopped cleanly. It is deliberately small: everything it shows is a
read-out of the same SQLite ledger the board reads, so the two can never
disagree.

Tkinter is imported lazily because the bridge also runs headless (``--no-gui``,
a service, a container) where the module may not be usable at all.
"""

from __future__ import annotations

import json
import queue
import webbrowser
from typing import Any

from . import contract
from .util import utc_now


class Gui:
    """Polls the ledger from the Tk main loop and renders a status panel."""

    def __init__(self, service: Any, base_url: str) -> None:
        self.service = service
        self.base_url = base_url.rstrip("/")
        self._cursor = 0
        self._root: Any = None
        self._tk: Any = None
        self._log: Any = None
        self._status_var: Any = None
        self._pause_var: Any = None
        self._lines: "queue.Queue[str]" = queue.Queue()

    # ------------------------------------------------------------------ build
    def run(self) -> None:
        import tkinter as tk
        from tkinter import scrolledtext

        self._tk = tk
        root = tk.Tk()
        self._root = root
        root.title("TradingBridge - signal board")
        root.geometry("760x560")
        root.configure(bg="#101418")

        header = tk.Frame(root, bg="#1b7f4b")
        header.pack(fill=tk.X)
        tk.Label(
            header,
            text="TradingBridge",
            font=("Segoe UI", 15, "bold"),
            bg="#1b7f4b",
            fg="white",
            pady=8,
            padx=12,
        ).pack(side=tk.LEFT)
        tk.Label(
            header,
            text=f"EA v{contract.EA_VERSION}  ·  schema v{contract.SCHEMA_VERSION}",
            bg="#1b7f4b",
            fg="#d6f2e2",
            padx=12,
        ).pack(side=tk.RIGHT)

        self._status_var = tk.StringVar(value="starting…")
        tk.Label(
            root,
            textvariable=self._status_var,
            justify=tk.LEFT,
            anchor="w",
            bg="#101418",
            fg="#8fd3a8",
            font=("Consolas", 9),
            padx=12,
            pady=6,
        ).pack(fill=tk.X)

        bar = tk.Frame(root, bg="#101418")
        bar.pack(fill=tk.X, padx=12)
        self._pause_var = tk.StringVar(value="Pause dispatch")
        tk.Button(bar, text="Open signal board", command=self._open_board, bg="#2d6cdf", fg="white",
                  relief=tk.FLAT, padx=10).pack(side=tk.LEFT, padx=(0, 6))
        tk.Button(bar, textvariable=self._pause_var, command=self._toggle_pause, bg="#b8860b", fg="white",
                  relief=tk.FLAT, padx=10).pack(side=tk.LEFT, padx=6)
        tk.Button(bar, text="Scan folder now", command=self._rescan, bg="#3a4a5a", fg="white",
                  relief=tk.FLAT, padx=10).pack(side=tk.LEFT, padx=6)
        tk.Button(bar, text="Clear queue", command=self._clear_queue, bg="#8b2b2b", fg="white",
                  relief=tk.FLAT, padx=10).pack(side=tk.LEFT, padx=6)

        self._log = scrolledtext.ScrolledText(
            root, height=24, font=("Consolas", 9), bg="#0b0e11", fg="#c9d1d9",
            insertbackground="#c9d1d9", relief=tk.FLAT, padx=8, pady=6,
        )
        self._log.pack(fill=tk.BOTH, expand=True, padx=12, pady=10)
        self._log.configure(state=tk.DISABLED)

        tk.Label(
            root,
            text="This software can place real orders on your broker account. Validate every change on a demo account first.",
            bg="#101418", fg="#e0a458", font=("Segoe UI", 8), pady=6,
        ).pack(fill=tk.X)

        self._append(f"inbox (read-only): {self.service.settings.signals_folder}")
        self._append(f"data folder:       {self.service.settings.data_folder}")
        self._append(f"database:          {self.service.settings.db_path}")
        self._append(f"listening on:      {self.base_url}")
        root.protocol("WM_DELETE_WINDOW", self._on_close)
        root.after(200, self._refresh)
        root.mainloop()

    # ---------------------------------------------------------------- actions
    def _open_board(self) -> None:
        webbrowser.open(self.base_url + "/")

    def _toggle_pause(self) -> None:
        paused = not self.service.settings.paused
        _, errors = self.service.update_settings({"paused": paused})
        if errors:
            self._append(f"could not change pause state: {errors}")
        self._pause_var.set("Resume dispatch" if paused else "Pause dispatch")
        self._append("dispatch PAUSED - intake keeps recording, nothing is sent to the EA"
                     if paused else "dispatch resumed")

    def _rescan(self) -> None:
        watcher = self.service.watcher
        if watcher is None:
            self._append("file intake is not running in this process")
            return
        results = watcher.scan_once()
        self._append(f"manual scan: {len(results)} file(s) claimed")

    def _clear_queue(self) -> None:
        result = self.service.clear_queue("cleared from the desktop panel")
        self._append(f"cleared {result['count']} queued signal(s); "
                     f"{result['still_dispatched']} already with the EA cannot be recalled")

    def _on_close(self) -> None:
        # Closing the panel stops the bridge. Leaving the server running with no
        # visible window is how an operator ends up with a process they forgot
        # was still trading. Raising here would be swallowed by Tk's callback
        # handler, so the main loop is exited and the caller shuts down.
        self._root.quit()
        self._root.destroy()

    # ------------------------------------------------------------------- view
    def _append(self, text: str) -> None:
        if self._log is None or self._tk is None:
            return
        tk = self._tk
        self._log.configure(state=tk.NORMAL)
        self._log.insert(tk.END, text.rstrip("\n") + "\n")
        # Keep the widget bounded: a bridge left running for weeks would
        # otherwise grow the text buffer without limit.
        if int(self._log.index("end-1c").split(".")[0]) > 2000:
            self._log.delete("1.0", "500.0")
        self._log.see(tk.END)
        self._log.configure(state=tk.DISABLED)

    def _refresh(self) -> None:
        try:
            self._poll_once()
        except Exception as exc:  # noqa: BLE001 - the panel must never crash the bridge
            self._append(f"panel refresh failed: {type(exc).__name__}: {exc}")
        if self._root is not None:
            self._root.after(1000, self._refresh)

    def _poll_once(self) -> None:
        service = self.service
        state = service.ea_state()
        depth = service.store.queue_depth()
        totals = service.store.metrics(3600)["totals"]
        by_status = totals["by_status"]
        watcher = service.watcher
        self._status_var.set(
            "EA: {ea}  ·  queue: {depth}  ·  executed {ok}  failed {bad}  skipped {skip}  rejected {rej}\n"
            "folder: {folder}  ·  pending {pending}  ·  {mode} validation  ·  {paused}".format(
                ea=("online" if state["online"] else
                    f"OFFLINE (last poll {state['seconds_since_poll']}s ago)"
                    if state["seconds_since_poll"] is not None else "never polled"),
                depth=depth,
                ok=by_status.get(contract.STATUS_EXECUTED, 0),
                bad=by_status.get(contract.STATUS_FAILED, 0),
                skip=by_status.get(contract.STATUS_SKIPPED, 0),
                rej=by_status.get(contract.STATUS_REJECTED, 0),
                folder=(watcher.status()["folder"] if watcher else "n/a"),
                pending=(watcher.status()["pending_files"] if watcher else 0),
                mode=service.settings.validation_mode,
                paused="PAUSED" if service.settings.paused else "live",
            )
        )
        self._pause_var.set("Resume dispatch" if service.settings.paused else "Pause dispatch")

        for event in service.store.list_events(since_id=self._cursor, limit=100):
            self._cursor = max(self._cursor, int(event["id"]))
            self._append(_format_event(event))


def _format_event(event: dict) -> str:
    """One ledger event as a log line."""
    kind = event.get("type", "?")
    rid = event.get("request_id") or "-"
    detail = event.get("detail")
    if isinstance(detail, str) and detail.startswith("{"):
        try:
            detail = json.loads(detail)
        except ValueError:
            pass
    text = ""
    if isinstance(detail, dict):
        text = detail.get("summary") or detail.get("error") or detail.get("reason") or ""
        extra = detail.get("result") or detail.get("retcode_label")
        if extra:
            text = f"{text} · {extra}" if text else str(extra)
        if detail.get("file"):
            text = f"{detail['file']} {text}".strip()
    elif isinstance(detail, str):
        text = detail
    stamp = str(event.get("created_at") or utc_now())[11:19]
    return f"[{stamp}] {kind:<18} {rid:<28} {text}"[:200]
