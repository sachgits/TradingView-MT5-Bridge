"""The SQLite ledger: FIFO claims, terminal states, and the event log."""
import json

from bridge import contract
from bridge.store import Store

from conftest import BUY


def insert(store, rid, command=None, status=contract.STATUS_QUEUED):
    return store.insert_signal(
        request_id=rid,
        dedup_hash=f"hash-{rid}",
        source=contract.SOURCE_API,
        command=command or {**BUY, "request_id_marker": rid},
        raw_payload={"action": "buy"},
        status=status,
    )


def test_claim_next_is_fifo_and_single_use(store):
    insert(store, "first")
    insert(store, "second")
    assert store.claim_next()["request_id"] == "first"
    assert store.claim_next()["request_id"] == "second"
    assert store.claim_next() is None


def test_a_claimed_signal_is_not_queued_anymore(store):
    insert(store, "only")
    store.claim_next()
    assert store.next_queued() == []
    assert store.get_signal("only")["status"] == contract.STATUS_DISPATCHED
    assert store.get_signal("only")["queue_ms"] is not None


def test_record_result_finishes_the_row(store):
    insert(store, "trade")
    store.claim_next()
    assert store.record_result("trade", status=contract.STATUS_EXECUTED,
                               retcode=10009, result_text="BUY OPENED")
    row = store.get_signal("trade")
    assert row["status"] == contract.STATUS_EXECUTED
    assert row["retcode"] == 10009
    assert row["result_text"] == "BUY OPENED"
    assert row["finished_at"]
    assert row["exec_ms"] is not None


def backdate(store, rid, column):
    store._conn.execute(
        f"UPDATE signals SET {column} = datetime('now', '-10 minutes') WHERE request_id = ?",
        (rid,),
    )
    store._conn.commit()


def test_expire_dispatched_never_resends(store):
    insert(store, "lost")
    store.claim_next()
    backdate(store, "lost", "dispatched_at")
    assert store.expire_dispatched(30) == ["lost"]
    assert store.get_signal("lost")["status"] == contract.STATUS_EXPIRED
    # Expiry is terminal: the command must not reappear on the queue.
    assert store.claim_next() is None


def test_expire_stale_retires_a_command_nobody_dispatched(store):
    insert(store, "old")
    backdate(store, "old", "created_at")
    assert store.expire_stale(30) == ["old"]
    assert store.get_signal("old")["status"] == contract.STATUS_EXPIRED


def test_revive_only_touches_retryable_statuses(store):
    insert(store, "done", status=contract.STATUS_EXECUTED)
    insert(store, "gone", status=contract.STATUS_EXPIRED)
    assert store.revive_signal("gone", command=BUY, raw_payload=BUY, dedup_hash="h") is True
    assert store.get_signal("gone")["status"] == contract.STATUS_QUEUED
    # An executed row is the record of a real trade and must never be rewritten.
    assert store.revive_signal("done", command=BUY, raw_payload=BUY, dedup_hash="h") is False
    assert store.get_signal("done")["status"] == contract.STATUS_EXECUTED


def test_find_duplicate_by_request_id_is_timeless(store):
    insert(store, "idem", status=contract.STATUS_EXECUTED)
    found = store.find_duplicate("different-hash", "idem", 0)
    assert found and found["request_id"] == "idem"


def test_find_duplicate_by_content_only_within_the_window(store):
    insert(store, "same", status=contract.STATUS_EXECUTED)
    assert store.find_duplicate("hash-same", "other", 60) is not None
    assert store.find_duplicate("hash-same", "other", 0) is None


def test_cancel_and_requeue(store):
    insert(store, "pending")
    assert store.cancel_signal("pending")
    assert store.get_signal("pending")["status"] == contract.STATUS_CANCELLED
    # Cancelling twice is harmless and leaves the row cancelled.
    store.cancel_signal("pending")
    assert store.get_signal("pending")["status"] == contract.STATUS_CANCELLED
    assert store.requeue_signal("pending")
    assert store.get_signal("pending")["status"] == contract.STATUS_QUEUED


def test_events_are_ordered_and_filterable(store):
    first = store.add_event("signal.received", "a", {"summary": "buy"})
    store.add_event("signal.result", "a", {"status": "executed"})
    store.add_event("ea.offline", None, {"seen_at": "x"})
    rows = store.list_events(since_id=first - 1)
    assert [r["type"] for r in rows] == ["signal.received", "signal.result", "ea.offline"]
    assert json.loads(rows[0]["detail"])["summary"] == "buy"
    assert [r["id"] for r in store.list_events(since_id=first)] == [first + 1, first + 2]
    assert len(store.list_events(request_id="a")) == 2


def test_metrics_aggregate_outcomes_and_retcodes(store):
    insert(store, "win")
    store.claim_next()
    store.record_result("win", status=contract.STATUS_EXECUTED, retcode=0, result_text="ok")
    insert(store, "loss")
    store.claim_next()
    store.record_result("loss", status=contract.STATUS_FAILED, retcode=10019, result_text="no money")
    metrics = store.metrics(since_seconds=3600)
    assert metrics["totals"]["by_status"]["executed"] == 1
    assert metrics["totals"]["by_status"]["failed"] == 1
    assert {r["retcode"]: r["count"] for r in metrics["by_retcode"]} == {0: 1, 10019: 1}
    labels = {r["retcode"]: r["label"] for r in metrics["by_retcode"]}
    assert labels == {0: "success", 10019: "no money"}
    assert metrics["latency_ms"]["samples"] == 2


def test_settings_round_trip(store):
    store.set_setting("watch_interval", "2.5")
    assert store.get_setting("watch_interval") == "2.5"
    assert store.load_settings(["watch_interval", "missing"]) == {"watch_interval": "2.5"}


def test_distinct_values_ignore_nulls(store):
    insert(store, "a", command={**BUY, "symbol": "EURUSD"})
    insert(store, "b", command={**BUY, "symbol": "XAUUSD"})
    assert store.distinct_values("symbol") == ["EURUSD", "XAUUSD"]


# --------------------------------------------------------------------------
# The intake register: the inbox is read-only, so this table is the memory of
# what has already been consumed. It is permanent, not windowed.
# --------------------------------------------------------------------------
def claim(store, path=r"C:\inbox\a.json", digest="aaa", size=10, mtime=100):
    return store.claim_file(source_path=path, content_hash=digest, size=size, mtime_ns=mtime)


def test_first_claim_wins_and_a_repeat_is_refused(store):
    assert claim(store) is None
    existing = claim(store)
    assert existing is not None
    assert existing["outcome"] == contract.INTAKE_OUTCOME_CLAIMED


def test_the_same_content_at_a_different_path_is_a_separate_claim(store):
    assert claim(store, path=r"C:\inbox\a.json") is None
    assert claim(store, path=r"C:\inbox\b.json") is None


def test_different_content_at_the_same_path_is_a_separate_claim(store):
    assert claim(store, digest="aaa") is None
    assert claim(store, digest="bbb") is None


def test_resolving_a_claim_records_the_outcome_and_the_copy(store):
    claim(store)
    store.resolve_file_claim(source_path=r"C:\inbox\a.json", content_hash="aaa",
                             outcome="queued", request_id="br-1", copy_name="a.processed")
    assert store.unresolved_file_claims() == []
    known = store.known_file_signatures()
    assert known[r"C:\inbox\a.json"] == (10, 100, "aaa")
    assert store.intake_counts()["queued"] == 1


def test_an_unresolved_claim_stays_visible_until_closed(store):
    claim(store)
    open_claims = store.unresolved_file_claims()
    assert len(open_claims) == 1
    assert open_claims[0]["outcome"] == contract.INTAKE_OUTCOME_CLAIMED
    store.resolve_file_claim(source_path=r"C:\inbox\a.json", content_hash="aaa",
                             outcome=contract.INTAKE_OUTCOME_INTERRUPTED)
    assert store.unresolved_file_claims() == []


def test_a_claim_survives_a_reopen_of_the_same_ledger(store, settings):
    claim(store)
    store.resolve_file_claim(source_path=r"C:\inbox\a.json", content_hash="aaa",
                             outcome="queued", copy_name="a.processed")
    reopened = Store(settings.db_path)
    try:
        assert reopened.known_file_signatures()[r"C:\inbox\a.json"] == (10, 100, "aaa")
        assert reopened.claim_file(source_path=r"C:\inbox\a.json", content_hash="aaa") is not None
    finally:
        reopened.close()


# --------------------------------------------------------------------------
# Adoption: files that were already in an inbox when we started watching it.
# Recorded by path, so an in-place update by their own application cannot turn
# them into a command.
# --------------------------------------------------------------------------
def test_adoption_records_paths_and_is_idempotent(store):
    entries = [(r"C:\inbox\a.json", 10, 100), (r"C:\inbox\b.json", 20, 200)]
    assert store.adopt_files(r"C:\inbox", entries) == 2
    assert store.adopt_files(r"C:\inbox", entries) == 0
    assert store.adopted_paths() == {r"C:\inbox\a.json", r"C:\inbox\b.json"}
    assert store.adoption_count() == 2


def test_adoption_survives_a_reopen_of_the_ledger(store, settings):
    store.adopt_files(r"C:\inbox", [(r"C:\inbox\a.json", 10, 100)])
    reopened = Store(settings.db_path)
    try:
        assert reopened.adopted_paths() == {r"C:\inbox\a.json"}
    finally:
        reopened.close()
