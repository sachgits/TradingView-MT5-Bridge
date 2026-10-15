"""Folder intake: the inbox is read-only, the bridge's copies carry the outcome."""
import json
import os
from pathlib import Path

from bridge import contract
from bridge.config import Settings
from bridge.service import Service
from bridge.watcher import FolderWatcher

from conftest import BUY


def watcher_for(service, settings):
    return FolderWatcher(
        service, settings.signals_folder, settings.intake_dir, settings.interrupted_dir
    )


def drop(settings, name, payload):
    path = settings.signals_folder / name
    path.write_text(payload if isinstance(payload, str) else json.dumps(payload), encoding="utf-8")
    return path


def age_mtime(path, delta_ns=2_000_000):
    """Make a rewrite's mtime distinct from the original, but still in the past.

    The watcher fast-skips a file whose (size, mtime_ns) still matches the
    register, so a rewrite that changes the content hash but not the byte length
    (EURUSD -> XAUUSD) is only told apart by its mtime. Two writes inside one
    filesystem timestamp tick collide and the new command is skipped.

    The mtime is pushed *backwards*, not forwards: with file_settle_ms=0 the
    scan skips a file whose mtime is in the future (now - mtime < 0), so aging
    it keeps the file settled while guaranteeing a distinct signature. Real
    producers write seconds apart; this models that deterministically instead of
    depending on the OS clock tick.
    """
    stat = path.stat()
    os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns - delta_ns))
    return path


def inbox_names(settings):
    return sorted(p.name for p in settings.signals_folder.iterdir())


def copy_names(settings):
    folder = settings.intake_dir
    return sorted(p.name for p in folder.iterdir()) if folder.is_dir() else []


def inbox_snapshot(settings):
    """Everything the bridge could be tempted to change about the inbox."""
    out = {}
    for path in sorted(settings.signals_folder.iterdir()):
        stat = path.stat()
        out[path.name] = (stat.st_size, stat.st_mtime_ns, path.read_bytes())
    return out


def test_a_good_file_is_queued_and_the_copy_is_marked(service, settings, store):
    drop(settings, "trade.json", BUY)
    results = watcher_for(service, settings).scan_once()
    assert len(results) == 1
    assert results[0]["status"] == contract.STATUS_QUEUED
    # The inbox keeps the original, untouched; the marking lands on our copy.
    assert inbox_names(settings) == ["trade.json"]
    assert copy_names(settings) == ["trade.processed"]
    assert store.queue_depth() == 1
    row = store.list_signals(limit=1)[0]
    assert row["source"] == contract.SOURCE_FILE
    assert row["source_file"] == "trade.json"


def test_the_inbox_is_never_modified(service, settings, store):
    drop(settings, "good.json", BUY)
    drop(settings, "broken.json", '{"action": "buy", "size":')
    drop(settings, "nosize.json", {"action": "buy", "symbol": "EURUSD"})
    before = inbox_snapshot(settings)

    watcher_for(service, settings).scan_once()

    assert inbox_snapshot(settings) == before
    assert inbox_names(settings) == ["broken.json", "good.json", "nosize.json"]
    assert sorted(copy_names(settings)) == [
        "broken.invalid", "good.processed", "nosize.rejected",
    ]


def test_an_unchanged_file_is_not_consumed_again(service, settings, store):
    drop(settings, "trade.json", BUY)
    watcher = watcher_for(service, settings)
    watcher.scan_once()
    assert watcher.scan_once() == []
    assert store.queue_depth() == 1


def test_a_restart_does_not_replay_the_inbox(service, settings, store):
    drop(settings, "trade.json", BUY)
    watcher_for(service, settings).scan_once()
    assert store.queue_depth() == 1

    # A new watcher over the same ledger: the file is still in the inbox, still
    # named .json, and must still not produce a second trade.
    fresh = watcher_for(service, settings)
    assert fresh.scan_once() == []
    assert store.queue_depth() == 1
    assert copy_names(settings) == ["trade.processed"]


def test_rewriting_the_same_name_with_new_content_is_a_new_command(service, settings, store):
    drop(settings, "trade.json", BUY)
    watcher = watcher_for(service, settings)
    watcher.scan_once()
    # XAUUSD is the same byte length as EURUSD, so only the content hash and the
    # mtime distinguish this rewrite; age the mtime so the stat fast-path sees
    # a changed file (see age_mtime).
    age_mtime(drop(settings, "trade.json", {**BUY, "symbol": "XAUUSD"}))
    results = watcher.scan_once()
    assert len(results) == 1
    assert store.queue_depth() == 2
    assert len(copy_names(settings)) == 2


def test_rewriting_the_same_name_with_identical_content_is_not(service, settings, store):
    drop(settings, "trade.json", BUY)
    watcher = watcher_for(service, settings)
    watcher.scan_once()
    drop(settings, "trade.json", BUY)
    assert watcher.scan_once() == []
    assert store.queue_depth() == 1


def test_a_corrupt_file_is_invalid_and_never_becomes_a_trade(service, settings, store):
    drop(settings, "broken.json", '{"action": "buy", "symbol": "EURUSD", "size":')
    results = watcher_for(service, settings).scan_once()
    assert results[0]["status"] == contract.STATUS_INVALID
    assert inbox_names(settings) == ["broken.json"]
    assert copy_names(settings) == ["broken.invalid"]
    # The word "buy" in a truncated file must not produce a market order.
    assert store.queue_depth() == 0


def test_legacy_text_in_a_json_file_is_invalid(service, settings, store):
    drop(settings, "legacy.json", "BUY SL=50 TP=100 LOT=0.01")
    results = watcher_for(service, settings).scan_once()
    assert results[0]["status"] == contract.STATUS_INVALID
    assert copy_names(settings) == ["legacy.invalid"]
    assert store.queue_depth() == 0


def test_a_rejected_command_is_marked_rejected(service, settings, store):
    drop(settings, "nosize.json", {"action": "buy", "symbol": "EURUSD"})
    results = watcher_for(service, settings).scan_once()
    assert results[0]["status"] == contract.STATUS_REJECTED
    assert inbox_names(settings) == ["nosize.json"]
    assert copy_names(settings) == ["nosize.rejected"]


def test_a_duplicate_command_under_a_new_name_is_not_queued_again(service, settings, store):
    drop(settings, "one.json", BUY)
    watcher = watcher_for(service, settings)
    watcher.scan_once()
    drop(settings, "two.json", BUY)
    results = watcher.scan_once()
    assert results[0]["status"] == "duplicate"
    assert copy_names(settings) == ["one.processed", "two.processed"]
    assert store.queue_depth() == 1


def test_copy_names_never_collide(service, settings, store):
    drop(settings, "same.json", BUY)
    watcher = watcher_for(service, settings)
    watcher.scan_once()
    (settings.intake_dir / "same.processed").write_text("occupied", encoding="utf-8")
    drop(settings, "same.json", {**BUY, "symbol": "XAUUSD"})
    watcher.scan_once()
    parked = [n for n in copy_names(settings) if n.startswith("same.processed")]
    assert len(parked) == 2, parked
    assert store.queue_depth() == 2


def test_an_unresolved_claim_is_reported_and_not_replayed(service, settings, store):
    drop(settings, "stuck.json", BUY)
    # Simulate a crash between claiming the file and recording an outcome: the
    # register row stays open and a half-finished copy is left in intake/.
    path = settings.signals_folder / "stuck.json"
    stat = path.stat()
    import hashlib
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    store.claim_file(source_path=str(path), content_hash=digest,
                     size=stat.st_size, mtime_ns=stat.st_mtime_ns)
    settings.intake_dir.mkdir(parents=True, exist_ok=True)
    leftover = settings.intake_dir / ("stuck" + contract.FILE_SUFFIX_PROCESSING)
    leftover.write_bytes(path.read_bytes())

    watcher = watcher_for(service, settings)
    watcher._recover_interrupted()

    parked = sorted(p.name for p in settings.interrupted_dir.iterdir())
    assert parked == ["stuck.interrupted"], parked
    assert copy_names(settings) == []
    # Replaying it automatically is exactly the double execution we prevent.
    assert store.queue_depth() == 0
    assert store.unresolved_file_claims() == []
    assert inbox_names(settings) == ["stuck.json"]
    assert any(e["type"] == "watcher" for e in store.list_events())

    # And a later scan must not pick the file up either.
    assert watcher.scan_once() == []
    assert store.queue_depth() == 0


def test_a_stray_copy_in_intake_is_set_aside(service, settings, store):
    settings.intake_dir.mkdir(parents=True, exist_ok=True)
    stray = settings.intake_dir / ("orphan" + contract.FILE_SUFFIX_PROCESSING)
    stray.write_text("{}", encoding="utf-8")
    watcher_for(service, settings)._recover_interrupted()
    assert sorted(p.name for p in settings.interrupted_dir.iterdir()) == ["orphan.interrupted"]
    assert copy_names(settings) == []


def test_disabled_intake_leaves_files_untouched(service, settings, store):
    service.settings.enable_file_intake = False
    drop(settings, "trade.json", BUY)
    assert watcher_for(service, settings).scan_once() == []
    assert inbox_names(settings) == ["trade.json"]
    assert store.queue_depth() == 0


def test_a_missing_inbox_is_created_not_fatal(service, settings, store):
    import shutil
    shutil.rmtree(settings.signals_folder)
    watcher = watcher_for(service, settings)
    assert watcher.scan_once() == []
    assert settings.signals_folder.is_dir()


def test_an_unreadable_inbox_is_not_fatal(service, settings, store, monkeypatch):
    drop(settings, "trade.json", BUY)
    watcher = watcher_for(service, settings)

    def deny(*args, **kwargs):
        raise PermissionError("inbox is read-only")

    monkeypatch.setattr("pathlib.Path.read_bytes", deny)
    assert watcher.scan_once() == []
    assert "read-only" in watcher.status()["last_error"]
    assert store.queue_depth() == 0


def test_status_reports_both_folders(service, settings, store):
    status = watcher_for(service, settings).status()
    assert status["folder"] == str(settings.signals_folder)
    assert status["data_folder"] == str(settings.data_folder)
    assert status["intake_dir"] == str(settings.intake_dir)
    assert status["inbox_read_only"] is True


def test_the_data_folder_may_not_be_the_inbox():
    settings = Settings()
    settings.signals_folder = Settings().signals_folder
    settings.data_folder = settings.signals_folder
    assert settings.path_conflict() is not None


def test_the_data_folder_may_not_live_inside_the_inbox(tmp_path):
    settings = Settings()
    settings.signals_folder = tmp_path / "signals"
    settings.data_folder = tmp_path / "signals" / "bridge"
    assert "inside the watch folder" in settings.path_conflict()


def test_the_inbox_may_not_live_inside_the_data_folder(tmp_path):
    settings = Settings()
    settings.data_folder = tmp_path / "bridge"
    settings.signals_folder = tmp_path / "bridge" / "signals"
    assert "inside the data folder" in settings.path_conflict()


def test_separate_folders_are_accepted(tmp_path):
    settings = Settings()
    settings.signals_folder = tmp_path / "signals"
    settings.data_folder = tmp_path / "bridge"
    assert settings.path_conflict() is None


def test_the_watcher_refuses_to_write_outside_its_own_folders(service, settings, store):
    watcher = watcher_for(service, settings)
    watcher._ensure_data_dirs()
    target = settings.signals_folder / "sneaky.processed"
    try:
        watcher._assert_ours(target)
    except OSError as exc:
        assert "outside the bridge data folder" in str(exc)
    else:  # pragma: no cover - the guard is the point of the test
        raise AssertionError("writing into the inbox was allowed")
    assert not os.path.exists(target)


# --------------------------------------------------------------------------
# The inbox is one of the settings on the board, so it can move while the
# bridge runs. Everything already in the new folder is adopted rather than
# traded: pointing the bridge at a folder of history must not fire it.
# --------------------------------------------------------------------------
def journal(settings, tmp_path, name="old.json", payload=None):
    folder = tmp_path / "journal"
    folder.mkdir(exist_ok=True)
    path = folder / name
    path.write_text(json.dumps(payload if payload is not None else BUY), encoding="utf-8")
    return folder


def switch(service, folder):
    public, errors = service.update_settings({"signals_folder": str(folder)})
    assert errors == {}, errors
    return public


def test_the_inbox_follows_the_setting(service, settings, tmp_path):
    watcher = watcher_for(service, settings)
    watcher.scan_once()
    folder = journal(settings, tmp_path)
    switch(service, folder)
    assert watcher.inbox == folder
    assert watcher.status()["folder"] == str(folder)


def test_switching_the_inbox_adopts_the_files_already_there(service, settings, store, tmp_path):
    watcher = watcher_for(service, settings)
    watcher.scan_once()
    folder = journal(settings, tmp_path)

    switch(service, folder)
    assert watcher.scan_once() == []
    assert store.queue_depth() == 0
    assert store.adopted_paths() == {str(folder / "old.json")}
    status = watcher.status()
    assert status["adopted_total"] == 1
    assert status["adopted_files"] == 1
    assert status["inbox_switches"] == 1
    # The inbox is still read-only: adopting records our decision, it does not
    # touch the file that belongs to somebody else, and nothing was copied in.
    assert sorted(p.name for p in folder.iterdir()) == ["old.json"]
    assert copy_names(settings) == []


def test_an_adopted_file_is_not_traded_even_after_it_changes(service, settings, store, tmp_path):
    """A shared inbox is often updated in place by its own application.

    Keying adoption on content would turn every such update into a new command;
    keying it on path means the file is simply not ours to trade.
    """
    watcher = watcher_for(service, settings)
    watcher.scan_once()
    folder = journal(settings, tmp_path)
    switch(service, folder)
    watcher.scan_once()

    (folder / "old.json").write_text(json.dumps(BUY), encoding="utf-8")
    os.utime(folder / "old.json", ns=(1, 1))
    assert watcher.scan_once() == []
    assert store.queue_depth() == 0


def test_a_file_written_after_the_switch_is_a_normal_command(service, settings, store, tmp_path):
    watcher = watcher_for(service, settings)
    watcher.scan_once()
    folder = journal(settings, tmp_path)
    switch(service, folder)
    watcher.scan_once()

    (folder / "new.json").write_text(json.dumps(BUY), encoding="utf-8")
    results = watcher.scan_once()
    assert len(results) == 1
    assert results[0]["status"] == contract.STATUS_QUEUED
    assert store.adopted_paths() == {str(folder / "old.json")}


def test_the_first_scan_does_not_adopt_what_was_waiting(service, settings, store, tmp_path):
    """A file dropped while the bridge was stopped is still a command.

    Only a *switch* adopts. Otherwise "start the bridge and it picks up what is
    waiting" would silently stop working.
    """
    folder = journal(settings, tmp_path)
    settings.signals_folder = folder
    watcher = watcher_for(service, settings)
    results = watcher.scan_once()
    assert len(results) == 1
    assert store.adopted_paths() == set()


def test_adoption_survives_a_restart(service, settings, store, tmp_path):
    watcher = watcher_for(service, settings)
    watcher.scan_once()
    folder = journal(settings, tmp_path)
    switch(service, folder)
    watcher.scan_once()

    restarted = watcher_for(service, settings)
    assert restarted.scan_once() == []
    assert store.queue_depth() == 0
    assert restarted.status()["adopted_total"] == 1


def test_a_relative_inbox_path_is_rejected(service, settings):
    before = settings.signals_folder
    public, errors = service.update_settings({"signals_folder": "signals"})
    assert "absolute" in errors["signals_folder"]
    assert settings.signals_folder == before


def test_an_inbox_inside_the_data_folder_is_rejected(service, settings, tmp_path):
    public, errors = service.update_settings({"signals_folder": str(settings.data_folder / "in")})
    assert "inside the data folder" in errors["signals_folder"]
    assert settings.signals_folder != settings.data_folder / "in"


def test_an_inbox_that_is_a_file_is_rejected(service, settings, tmp_path):
    a_file = tmp_path / "not-a-folder.json"
    a_file.write_text("{}", encoding="utf-8")
    _, errors = service.update_settings({"signals_folder": str(a_file)})
    assert "not a folder" in errors["signals_folder"]


def test_a_bad_inbox_does_not_discard_the_rest_of_the_form(service, settings):
    """All-or-nothing: one bad field means none of them change."""
    _, errors = service.update_settings({"signals_folder": "nope", "watch_interval": 3.0})
    assert errors
    assert settings.watch_interval != 3.0


def test_the_inbox_setting_is_persisted_as_a_path(service, settings, store, tmp_path):
    folder = tmp_path / "journal2"
    folder.mkdir()
    switch(service, folder)
    assert store.get_setting("signals_folder") == str(folder)

    fresh = Settings()
    fresh.data_folder = settings.data_folder
    fresh.db_path = settings.db_path
    Service(fresh, store, None)
    assert fresh.signals_folder == folder
    assert isinstance(fresh.signals_folder, Path)


def test_a_pinned_inbox_is_not_overwritten_by_the_ledger(settings, store, tmp_path):
    """--folder means what it says, even if the board saved something else."""
    folder = tmp_path / "journal3"
    folder.mkdir()
    settings.apply({"signals_folder": str(folder)})
    store.set_setting("signals_folder", str(folder))

    pinned = Settings()
    pinned.data_folder = settings.data_folder
    pinned.db_path = settings.db_path
    cli = tmp_path / "from-the-command-line"
    cli.mkdir()
    pinned.signals_folder = cli
    pinned.pinned.add("signals_folder")
    Service(pinned, store, None)
    assert pinned.signals_folder == cli
