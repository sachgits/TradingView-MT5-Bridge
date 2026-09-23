"""In-memory fan-out of ledger events to connected dashboards.

The SQLite ``events`` table is the source of truth; this bus exists only so a
browser does not have to poll it. Every event is written to the store first and
published here afterwards, which means a subscriber that connects late, drops
events, or disconnects entirely loses nothing - it re-reads the table from its
last seen id.

That property is what makes Server-Sent Events safe here. Waitress buffers
generator responses and holds one worker thread per open stream, so a client
that stalls or vanishes must never be able to block intake or dispatch. Every
operation on this bus is therefore non-blocking and bounded: a full subscriber
queue drops its oldest event rather than its producer.
"""

from __future__ import annotations

import queue
import threading
from typing import Any, Dict, List, Optional

# Event types. Namespaced so the dashboard can subscribe to "signal.*".
SIGNAL_RECEIVED = "signal.received"
SIGNAL_REJECTED = "signal.rejected"
SIGNAL_INVALID = "signal.invalid"
SIGNAL_DUPLICATE = "signal.duplicate"
SIGNAL_DISPATCHED = "signal.dispatched"
SIGNAL_RESULT = "signal.result"
SIGNAL_EXPIRED = "signal.expired"
SIGNAL_CANCELLED = "signal.cancelled"
SIGNAL_REQUEUED = "signal.requeued"
EA_ONLINE = "ea.online"
EA_OFFLINE = "ea.offline"
SETTINGS_CHANGED = "settings.changed"
WATCHER = "watcher"


class EventBus:
    """Bounded publish/subscribe for dashboard clients."""

    def __init__(self, max_clients: int = 8, backlog: int = 500) -> None:
        # Waitress serves each SSE stream from its own worker thread, so the
        # client cap is what keeps the thread pool from being exhausted by
        # browsers. Beyond it, /api/v1/stream answers 503 and the dashboard
        # falls back to polling /api/v1/events.
        self.max_clients = max_clients
        self.backlog = backlog
        self._subscribers: List["queue.Queue[Dict[str, Any]]"] = []
        self._lock = threading.Lock()

    def subscribe(self) -> Optional["queue.Queue[Dict[str, Any]]"]:
        with self._lock:
            if len(self._subscribers) >= self.max_clients:
                return None
            sub: "queue.Queue[Dict[str, Any]]" = queue.Queue(maxsize=self.backlog)
            self._subscribers.append(sub)
            return sub

    def unsubscribe(self, sub: "queue.Queue[Dict[str, Any]]") -> None:
        with self._lock:
            if sub in self._subscribers:
                self._subscribers.remove(sub)

    def publish(self, event: Dict[str, Any]) -> None:
        with self._lock:
            targets = list(self._subscribers)
        for sub in targets:
            try:
                sub.put_nowait(event)
            except queue.Full:
                # A slow consumer must not apply back-pressure to intake.
                try:
                    sub.get_nowait()
                    sub.put_nowait(event)
                except queue.Empty:
                    pass
                except queue.Full:
                    pass

    @property
    def client_count(self) -> int:
        with self._lock:
            return len(self._subscribers)
