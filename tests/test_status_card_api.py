from __future__ import annotations

import sys
import types

from fastapi import FastAPI
from fastapi.testclient import TestClient

from adaos.apps.api.auth import require_token
from adaos.domain import make_client_subscription_record, make_projection_subscription
from adaos.services.projection_demand import clear_projection_demand_registry, write_client_subscription_record
from adaos.services.projection_dispatcher import clear_projection_dispatcher
from adaos.services.status_card_registry import clear_status_card_registry


def _make_client() -> TestClient:
    sys.modules.setdefault("nats", types.SimpleNamespace())
    fake_y_py = types.SimpleNamespace(
        YDoc=type("YDoc", (), {}),
        apply_update=lambda *args, **kwargs: None,
    )
    sys.modules.setdefault("y_py", fake_y_py)
    fake_ystore_module = types.ModuleType("ypy_websocket.ystore")
    fake_ystore_module.BaseYStore = object
    fake_ystore_module.YDocNotFound = RuntimeError
    fake_ypy_websocket = types.ModuleType("ypy_websocket")
    fake_ypy_websocket.ystore = fake_ystore_module
    sys.modules.setdefault("ypy_websocket", fake_ypy_websocket)
    sys.modules.setdefault("ypy_websocket.ystore", fake_ystore_module)

    from adaos.apps.api import node_api

    clear_projection_demand_registry()
    clear_projection_dispatcher()
    clear_status_card_registry()
    app = FastAPI()
    app.include_router(node_api.router, prefix="/api/node")
    app.dependency_overrides[require_token] = lambda: None
    return TestClient(app)


def _sample_infrascope_snapshot() -> dict:
    return {
        "summary": {
            "label": "Infrascope",
            "value": "nominal",
            "subtitle": "operator view",
        },
        "overview": {
            "active_incidents": [],
            "active_runtimes": [{"id": "runtime-a", "status": "online"}],
        },
        "inventory": {
            "all": [
                {"id": "browser-a", "kind": "browser", "status": "online"},
                {"id": "skill-a", "kind": "skill", "status": "online"},
            ],
            "browsers": [{"id": "browser-a", "status": "online"}],
            "runtimes": [{"id": "runtime-a", "status": "online"}],
            "skills": [{"id": "skill-a", "status": "online"}],
            "scenarios": [{"id": "desktop", "status": "online"}],
        },
        "operations": {"active": []},
    }


def test_status_card_api_publishes_and_reads_projection() -> None:
    client = _make_client()

    publish_resp = client.post(
        "/api/node/status-cards",
        json={
            "id": "runtime",
            "owner": "core:runtime",
            "kind": "runtime",
            "scope": {"node_id": "node-a"},
            "webspace_id": "desktop",
            "status": "running",
            "summary": "Runtime ready",
            "updated_at": 10.0,
        },
    )
    projection_resp = client.get(
        "/api/node/status-cards/runtime/projection",
        params={"webspace_id": "desktop"},
    )

    assert publish_resp.status_code == 200
    assert publish_resp.json()["card"]["version"] == 1
    assert projection_resp.status_code == 200
    payload = projection_resp.json()
    assert payload["record"]["status"] == "ready"
    assert payload["record"]["data"]["summary"] == "Runtime ready"
    assert payload["record"]["meta"]["projection_key"] == "status-card:runtime"


def test_status_card_api_snapshot_includes_runtime_card_by_default() -> None:
    client = _make_client()

    resp = client.get("/api/node/status-cards", params={"webspace_id": "desktop"})

    assert resp.status_code == 200
    payload = resp.json()
    assert payload["card_total"] == 1
    assert payload["cards"][0]["id"] == "runtime"
    assert payload["records"][0]["meta"]["projection_key"] == "status-card:runtime"
    assert payload["stats"]["publish_total"] == 1


def test_status_card_api_can_refresh_runtime_card_explicitly() -> None:
    client = _make_client()

    resp = client.post("/api/node/status-cards/runtime/refresh", params={"webspace_id": "desktop"})

    assert resp.status_code == 200
    payload = resp.json()
    assert payload["card"]["id"] == "runtime"
    assert payload["card"]["owner"] == "core:runtime"
    assert payload["snapshot"]["projection_total"] == 1


def test_status_card_api_refreshes_infrascope_cards_from_request() -> None:
    client = _make_client()

    resp = client.post(
        "/api/node/status-cards/infrascope/refresh",
        json={"webspace_id": "desktop", "snapshot": _sample_infrascope_snapshot()},
    )
    projection_resp = client.get(
        "/api/node/status-cards/infrascope-overview/projection",
        params={"webspace_id": "desktop"},
    )

    assert resp.status_code == 200
    payload = resp.json()
    assert payload["source"] == "request"
    assert payload["card_total"] == 7
    assert payload["snapshot"]["card_total"] == 7
    assert {card["id"] for card in payload["cards"]} >= {
        "infrascope-overview",
        "infrascope-inventory",
        "infrascope-registry",
    }
    assert projection_resp.status_code == 200
    projection = projection_resp.json()["record"]
    assert projection["data"]["status"] == "online"
    assert projection["data"]["summary"] == "Infrascope | nominal | operator view"


def test_status_card_api_refreshes_infrascope_cards_from_yjs_snapshot(monkeypatch) -> None:
    client = _make_client()

    class FakeYDoc:
        def get_map(self, name):
            assert name == "data"
            return {"infrascope": _sample_infrascope_snapshot()}

    class FakeReadContext:
        async def __aenter__(self):
            return FakeYDoc()

        async def __aexit__(self, exc_type, exc, tb):
            return False

    from adaos.apps.api import node_api

    monkeypatch.setattr(node_api, "async_read_ydoc", lambda _webspace_id: FakeReadContext())

    resp = client.post(
        "/api/node/status-cards/infrascope/refresh",
        params={"webspace_id": "desktop"},
        json={},
    )

    assert resp.status_code == 200
    payload = resp.json()
    assert payload["source"] == "data/infrascope"
    assert payload["card_total"] == 7
    assert payload["snapshot"]["projection_total"] == 7


def test_status_card_api_sweeps_stale_cards() -> None:
    client = _make_client()
    client.post(
        "/api/node/status-cards",
        json={
            "id": "runtime",
            "owner": "core:runtime",
            "kind": "runtime",
            "scope": {"node_id": "node-a"},
            "webspace_id": "desktop",
            "status": "running",
            "summary": "Runtime ready",
            "ttl_ms": 5000,
            "updated_at": 10.0,
        },
    )

    preview_resp = client.post(
        "/api/node/status-cards/sweep",
        params={"webspace_id": "desktop", "now": 20.0, "dry_run": "true"},
    )
    sweep_resp = client.post(
        "/api/node/status-cards/sweep",
        params={"webspace_id": "desktop", "now": 20.0},
    )

    assert preview_resp.status_code == 200
    assert preview_resp.json()["removed_total"] == 0
    assert preview_resp.json()["stale_total"] == 1
    assert sweep_resp.status_code == 200
    payload = sweep_resp.json()
    assert payload["removed_total"] == 1
    assert payload["snapshot"]["card_total"] == 0


def test_status_card_api_dispatches_materialized_demand() -> None:
    client = _make_client()
    client.post(
        "/api/node/status-cards",
        json={
            "id": "runtime",
            "owner": "core:runtime",
            "kind": "runtime",
            "scope": {"node_id": "node-a"},
            "webspace_id": "desktop",
            "status": "running",
            "summary": "Runtime ready",
            "updated_at": 10.0,
        },
    )
    write_client_subscription_record(
        make_client_subscription_record(
            client_id="browser-1",
            device_id="desktop",
            session_id="session-1",
            webspace_id="desktop",
            role="operator",
            subscriptions=[
                make_projection_subscription(
                    projection_key="status-card:runtime",
                    consumer_id="widget:runtime",
                    consumer_kind="widget",
                )
            ],
        )
    )

    resp = client.post(
        "/api/node/projection-dispatcher/dispatch",
        json={
            "type": "node.status",
            "payload": {"webspace_id": "desktop"},
            "source": "test",
        },
    )

    assert resp.status_code == 200
    payload = resp.json()
    assert payload["report"]["refreshed"][0]["status"] == "ready"
    assert payload["report"]["refreshed"][0]["record"]["data"]["status"] == "online"
    assert payload["dispatcher"]["stats"]["refreshed_total"] == 1


def test_status_card_details_refresh_requests_stream_snapshot(monkeypatch) -> None:
    client = _make_client()
    events = []

    class Bus:
        def publish(self, event):
            events.append(event)

    from adaos.apps.api import node_api

    monkeypatch.setattr(node_api, "get_ctx", lambda: types.SimpleNamespace(bus=Bus()))
    client.post(
        "/api/node/status-cards",
        json={
            "id": "infrastate-yjs",
            "owner": "skill:infrastate_skill",
            "kind": "yjs",
            "scope": {"section": "yjs"},
            "webspace_id": "desktop",
            "status": "running",
            "summary": "Yjs state nominal",
            "details_ref": {
                "kind": "stream",
                "receiver": "infrastate.yjs.load_mark",
                "params": {"webspace_id": "desktop"},
            },
        },
    )

    resp = client.post(
        "/api/node/status-cards/infrastate-yjs/details/refresh",
        params={"webspace_id": "desktop"},
    )

    assert resp.status_code == 200
    payload = resp.json()
    assert payload["accepted"] is True
    assert payload["details_ref"]["receiver"] == "infrastate.yjs.load_mark"
    assert payload["requested_event"]["type"] == "webio.stream.snapshot.requested"
    assert events[0].payload["receiver"] == "infrastate.yjs.load_mark"
    assert events[0].payload["card_id"] == "infrastate-yjs"


def test_status_card_details_refresh_reports_api_details_ref() -> None:
    client = _make_client()
    client.post(
        "/api/node/status-cards",
        json={
            "id": "infrastate-summary",
            "owner": "skill:infrastate_skill",
            "kind": "infrastate",
            "scope": {"section": "summary"},
            "webspace_id": "desktop",
            "status": "running",
            "summary": "Infra State",
            "details_ref": {
                "kind": "api",
                "path": "/api/node/infrastate/snapshot",
                "params": {"webspace_id": "desktop"},
            },
        },
    )

    resp = client.post(
        "/api/node/status-cards/infrastate-summary/details/refresh",
        params={"webspace_id": "desktop"},
    )

    assert resp.status_code == 200
    payload = resp.json()
    assert payload["accepted"] is False
    assert payload["reason"] == "api_details_ref"
    assert payload["details_ref"]["path"] == "/api/node/infrastate/snapshot"
