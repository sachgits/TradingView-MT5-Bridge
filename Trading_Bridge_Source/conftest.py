"""Shared fixtures. Run pytest from Trading_Bridge_Source: python -m pytest."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import pytest

from bridge.config import Settings
from bridge.events import EventBus
from bridge.service import Service
from bridge.store import Store


@pytest.fixture()
def settings(tmp_path):
    settings = Settings()
    # The inbox is shared with other producers and is read-only as far as the
    # bridge is concerned; everything the bridge writes goes under data_folder.
    settings.signals_folder = tmp_path / "signals"
    settings.signals_folder.mkdir()
    settings.data_folder = tmp_path / "bridge-data"
    settings.db_path = tmp_path / "bridge.db"
    settings.watch_interval = 0.2
    settings.file_settle_ms = 0
    settings.dedup_window_seconds = 60
    return settings


@pytest.fixture()
def store(settings):
    store = Store(settings.db_path)
    yield store
    store.close()


@pytest.fixture()
def service(settings, store):
    return Service(settings, store, EventBus())


BUY = {
    "action": "buy",
    "symbol": "EURUSD",
    "size": {"value": 0.01, "unit": "lots"},
    "stop_loss": {"unit": "points", "value": 200},
    "take_profit": {"unit": "points", "value": 400},
}
