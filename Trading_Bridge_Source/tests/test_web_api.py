"""The HTTP surface: EA routes, operator API, origin guard, and the SSE cap."""
import json

import pytest

from bridge import contract
from bridge.events import EventBus
from bridge.service import Service
from bridge.web import create_app

from conftest import BUY


@pytest.fixture()
def client_or_service(service):
    return client_for(service), service


def client_for(service):
    app = create_app(service)
    app.config["TESTING"] = True
    return app.test_client()


def post_signal(client, payload=BUY, origin=None):
    headers = {"Content-Type": "application/json"}
    if origin:
        headers["Origin"] = origin
    return client.post("/api/v1/signals", data=json.dumps({"command": payload}), headers=headers)


def test_health_reports_the_queue(client_or_service):
    client, _ = client_or_service
    body = client.get("/health").get_json()
    assert body["status"] == "running"
    assert body["schema_version"] == contract.SCHEMA_VERSION


def test_a_cross_origin_browser_cannot_submit(client_or_service):
    client, _ = client_or_service
    response = post_signal(client, origin="https://evil.example")
    assert response.status_code == 403


def test_the_board_itself_is_first_party(client_or_service):
    client, _ = client_or_service
    assert post_signal(client, origin="http://localhost").status_code == 201


def test_a_non_browser_client_sends_no_origin(client_or_service):
    client, _ = client_or_service
    assert post_signal(client).status_code == 201


def test_the_ea_cycle_over_http(client_or_service):
    client, service = client_or_service
    store = service.store
    rid = post_signal(client).get_json()["request_id"]
    envelope = client.get("/signal").get_json()
    assert envelope["status"] == contract.ENVELOPE_NEW
    assert envelope["request_id"] == rid
    assert list(envelope.keys()) == list(contract.ENVELOPE_KEYS)

    ack = client.post("/signal/processed",
                      data=json.dumps({"request_id": rid, "status": "SUCCESS", "retcode": 0}),
                      headers={"Content-Type": "application/json"})
    assert ack.status_code == 200
    assert store.get_signal(rid)["status"] == contract.STATUS_EXECUTED


def test_validate_is_a_dry_run(client_or_service):
    client, service = client_or_service
    store = service.store
    body = client.post("/api/v1/validate",
                       data=json.dumps(BUY),
                       headers={"Content-Type": "application/json"}).get_json()
    assert body["valid"] is True
    assert list(body["envelope"].keys()) == list(contract.ENVELOPE_KEYS)
    assert store.queue_depth() == 0


def test_signal_detail_carries_its_timeline(client_or_service):
    client, _ = client_or_service
    rid = post_signal(client).get_json()["request_id"]
    client.get("/signal")
    detail = client.get(f"/api/v1/signals/{rid}").get_json()
    assert detail["request_id"] == rid
    assert [e["type"] for e in detail["events"]][0] == "signal.received"
    assert "_meta" not in detail["command"]


def test_settings_patch_is_all_or_nothing_over_http(client_or_service):
    client, service = client_or_service
    response = client.patch("/api/v1/settings",
                            data=json.dumps({"watch_interval": "99999", "paused": "true"}),
                            headers={"Content-Type": "application/json"})
    assert response.status_code == 422
    assert service.settings.paused is False
    assert client.patch("/api/v1/settings",
                        data=json.dumps({"watch_interval": "4"}),
                        headers={"Content-Type": "application/json"}).status_code == 200
    assert service.settings.watch_interval == 4.0


def test_the_intake_token_is_never_echoed(client_or_service):
    client, service = client_or_service
    service.settings.intake_token = "secret"
    body = client.get("/api/v1/settings").get_json()
    assert "secret" not in json.dumps(body)
    # And a request without the token is now refused.
    assert post_signal(client).status_code == 401
    service.settings.intake_token = ""


def test_unknown_routes_answer_json_not_html(client_or_service):
    client, _ = client_or_service
    response = client.get("/api/v1/nope")
    assert response.status_code == 404
    assert response.get_json()["ok"] is False


def test_the_dashboard_is_served(client_or_service):
    client, _ = client_or_service
    response = client.get("/")
    assert response.status_code == 200
    assert b"TradingBridge" in response.data


def test_the_stream_refuses_clients_beyond_the_cap(settings, store):
    service = Service(settings, store, EventBus(max_clients=1))
    client = client_for(service)
    taken = service.bus.subscribe()
    assert taken is not None
    response = client.get("/api/v1/stream")
    assert response.status_code == 503
    assert response.headers["Retry-After"] == "5"
    service.bus.unsubscribe(taken)
    assert client.get("/api/v1/stream").status_code == 200


def test_the_inbox_is_editable_over_http(client_or_service, tmp_path):
    client, service = client_or_service
    folder = tmp_path / "other-inbox"
    folder.mkdir()
    response = client.patch("/api/v1/settings", json={"signals_folder": str(folder)})
    assert response.status_code == 200
    body = response.get_json()
    assert body["ok"] is True
    assert body["settings"]["signals_folder"] == str(folder)
    assert service.settings.signals_folder == folder


def test_a_bad_inbox_is_refused_over_http_and_nothing_changes(client_or_service, settings):
    client, service = client_or_service
    before = settings.signals_folder
    response = client.patch("/api/v1/settings",
                            json={"signals_folder": "signals", "watch_interval": 7.0})
    assert response.status_code == 422
    errors = response.get_json()["errors"]
    assert "absolute" in errors["signals_folder"]
    # All-or-nothing: the valid field in the same request did not take effect.
    assert settings.watch_interval != 7.0
    assert settings.signals_folder == before


def test_the_inbox_is_advertised_as_a_path_setting(client_or_service):
    client, _ = client_or_service
    editable = client.get("/api/v1/settings").get_json()["editable"]["signals_folder"]
    assert editable["type"] == "string"
    assert editable["path"] is True
    assert editable["label"]
