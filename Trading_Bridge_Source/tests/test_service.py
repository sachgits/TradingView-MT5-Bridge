"""The dispatch policy: never invent a parameter, never send a command twice."""
import json

from bridge import contract, events

from conftest import BUY


def queued_ids(store):
    return [row["request_id"] for row in store.next_queued(limit=100)]


def test_submit_queues_and_announces(service, store):
    result = service.submit(dict(BUY))
    assert result["ok"] is True
    assert result["status"] == contract.STATUS_QUEUED
    assert store.queue_depth() == 1
    types = [e["type"] for e in store.list_events()]
    assert types == [events.SIGNAL_RECEIVED]


def test_a_rejected_command_still_gets_a_ledger_row(service, store):
    result = service.submit({"action": "buy", "symbol": "EURUSD"})
    assert result["ok"] is False
    assert result["status"] == contract.STATUS_REJECTED
    assert store.queue_depth() == 0
    row = store.get_signal(result["request_id"])
    assert row["status"] == contract.STATUS_REJECTED
    assert "size" in row["error"]


def test_unparseable_input_is_invalid_not_legacy(service, store):
    result = service.submit('{"action": "buy", "symbol":')
    assert result["status"] == contract.STATUS_INVALID
    assert store.queue_depth() == 0


def test_the_same_command_is_never_queued_twice(service, store):
    first = service.submit(dict(BUY))
    second = service.submit(dict(BUY))
    assert second["ok"] is False
    assert second["status"] == "duplicate"
    assert second["duplicate_of"] == first["request_id"]
    assert store.queue_depth() == 1


def test_a_reused_request_id_revives_a_retryable_row(service, store):
    first = service.submit(dict(BUY), request_id="idem-1")
    service.cancel("idem-1")
    again = service.submit(dict(BUY), request_id="idem-1")
    assert again["ok"] is True
    assert again.get("revived") is True
    assert again["request_id"] == "idem-1"
    assert store.get_signal("idem-1")["status"] == contract.STATUS_QUEUED


def test_a_reused_request_id_on_an_executed_trade_is_a_duplicate(service, store):
    service.submit(dict(BUY), request_id="idem-2")
    service.ea_poll()
    service.ea_ack({"request_id": "idem-2", "status": "SUCCESS", "retcode": 0})
    again = service.submit(dict(BUY), request_id="idem-2")
    assert again["ok"] is False
    assert store.get_signal("idem-2")["status"] == contract.STATUS_EXECUTED


def test_envelope_key_order_protects_the_ea_parser(service, store):
    service.submit(dict(BUY))
    envelope = service.ea_poll()
    assert list(envelope.keys()) == list(contract.ENVELOPE_KEYS)
    assert envelope["status"] == contract.ENVELOPE_NEW
    # _meta is bridge-internal and must never reach the EA's text parser.
    assert "_meta" not in envelope["normalized"]
    assert "_meta" not in json.loads(envelope["signal"])
    # The escaped copy does contain the literal text "action"; what protects the
    # EA is that the first textual occurrence of every command key lies inside
    # "normalized", which precedes "signal" on the wire.
    text = json.dumps(envelope, separators=(",", ":"))
    assert text.index('"normalized"') < text.index('"action"') < text.index('"signal"')


def test_a_second_poll_while_dispatched_gets_nothing(service, store):
    service.submit(dict(BUY))
    assert service.ea_poll()["status"] == contract.ENVELOPE_NEW
    idle = service.ea_poll()
    assert idle["status"] == contract.ENVELOPE_IDLE
    assert idle["signal"] == "NONE"


def test_pause_holds_the_queue(service, store):
    service.submit(dict(BUY))
    service.update_settings({"paused": True})
    envelope = service.ea_poll()
    assert envelope["status"] == contract.ENVELOPE_IDLE
    assert envelope.get("reason") == "paused"
    assert store.queue_depth() == 1


def test_ack_statuses_map_to_ledger_statuses(service, store):
    for index, (ack, expected) in enumerate((("SUCCESS", contract.STATUS_EXECUTED),
                                             ("FAILED", contract.STATUS_FAILED),
                                             ("SKIPPED", contract.STATUS_SKIPPED))):
        rid = service.submit({**BUY, "symbol": f"PAIR{index}"})["request_id"]
        service.ea_poll()
        service.ea_ack({"request_id": rid, "status": ack, "retcode": 0 if ack == "SUCCESS" else -13})
        assert store.get_signal(rid)["status"] == expected


def test_an_unrecognised_ack_status_is_recorded_as_a_failure(service, store):
    rid = service.submit(dict(BUY))["request_id"]
    service.ea_poll()
    service.ea_ack({"request_id": rid, "status": "CONFUSED", "retcode": 42})
    assert store.get_signal(rid)["status"] == contract.STATUS_FAILED
    assert store.get_signal(rid)["retcode"] == 42


def test_an_ack_without_request_id_is_refused_loudly(service, store):
    result = service.ea_ack({"status": "SUCCESS"})
    assert result["ok"] is False
    assert any(e["type"] == events.SIGNAL_RESULT for e in store.list_events())


def test_a_late_ack_supersedes_the_expiry(service, store):
    rid = service.submit(dict(BUY))["request_id"]
    service.ea_poll()
    store._conn.execute(
        "UPDATE signals SET dispatched_at = datetime('now', '-10 minutes') WHERE request_id = ?",
        (rid,),
    )
    store._conn.commit()
    assert store.expire_dispatched(30) == [rid]
    assert store.get_signal(rid)["status"] == contract.STATUS_EXPIRED
    service.ea_ack({"request_id": rid, "status": "SUCCESS", "retcode": 0})
    # The position is open whether or not the bridge was still watching.
    assert store.get_signal(rid)["status"] == contract.STATUS_EXECUTED


def test_preview_writes_nothing(service, store):
    preview = service.preview(dict(BUY))
    assert preview["valid"] is True
    assert preview["envelope"]["normalized"]["action"] == "buy"
    assert store.queue_depth() == 0
    assert store.list_events() == []


def test_update_settings_is_all_or_nothing(service, store):
    view, errors = service.update_settings({"watch_interval": "99999", "paused": "true"})
    assert errors and "watch_interval" in errors
    assert service.settings.paused is False
    view, errors = service.update_settings({"watch_interval": "2", "paused": "true"})
    assert errors == {}
    assert service.settings.watch_interval == 2.0
    assert service.settings.paused is True


def test_clear_queue_retires_everything_waiting(service, store):
    service.submit(dict(BUY))
    service.submit({**BUY, "symbol": "XAUUSD"})
    result = service.clear_queue("test")
    assert result["count"] == 2
    assert store.queue_depth() == 0


def test_retry_mints_a_new_request_id(service, store):
    rid = service.submit(dict(BUY))["request_id"]
    service.ea_poll()
    service.ea_ack({"request_id": rid, "status": "FAILED", "retcode": 10019})
    result = service.retry(rid)
    assert result["ok"] is True
    assert result["request_id"] != rid
    assert store.queue_depth() == 1


def test_queue_depth_limit_refuses_new_work(service, store):
    service.settings.max_queue_depth = 1
    assert service.submit(dict(BUY))["ok"] is True
    refused = service.submit({**BUY, "symbol": "XAUUSD"})
    assert refused["ok"] is False
    assert "queue full" in refused["errors"][0]
