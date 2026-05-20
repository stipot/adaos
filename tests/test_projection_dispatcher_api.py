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


def test_projection_dispatcher_snapshot_endpoint_is_empty_by_default() -> None:
    client = _make_client()

    resp = client.get("/api/node/projection-dispatcher")

    assert resp.status_code == 200
    payload = resp.json()
    assert payload["ok"] is True
    assert payload["handler_total"] == 2
    assert payload["handlers"] == ["status-card:*", "status-card:infrascope-*"]
    assert payload["stats"]["incoming_total"] == 0


def test_projection_dispatcher_dispatch_endpoint_selects_demanded_projection() -> None:
    client = _make_client()
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
    assert payload["ok"] is True
    assert payload["report"]["selected"][0]["projection_key"] == "status-card:runtime"
    assert payload["report"]["refreshed"][0]["status"] == "unavailable"
    assert payload["report"]["refreshed"][0]["reason"] == "status_card_missing"
    assert payload["dispatcher"]["stats"]["incoming_total"] == 1
    assert payload["dispatcher"]["stats"]["refreshed_total"] == 1
    assert payload["dispatcher"]["lifecycle"][0]["status"] == "unavailable"


def test_projection_dispatcher_refreshes_demanded_infrascope_card_from_yjs(monkeypatch) -> None:
    client = _make_client()
    write_client_subscription_record(
        make_client_subscription_record(
            client_id="browser-1",
            device_id="desktop",
            session_id="session-1",
            webspace_id="desktop",
            role="operator",
            subscriptions=[
                make_projection_subscription(
                    projection_key="status-card:infrascope-overview",
                    consumer_id="widget:infrascope",
                    consumer_kind="widget",
                )
            ],
        )
    )

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
        "/api/node/projection-dispatcher/dispatch",
        json={
            "type": "infrascope.snapshot.changed",
            "payload": {"webspace_id": "desktop"},
            "source": "test",
        },
    )

    assert resp.status_code == 200
    payload = resp.json()
    refreshed = payload["report"]["refreshed"][0]
    assert payload["report"]["selected"][0]["projection_key"] == "status-card:infrascope-overview"
    assert refreshed["status"] == "ready"
    assert refreshed["reason"] == "materialized"
    assert refreshed["record"]["data"]["summary"] == "Infrascope | nominal | operator view"
    assert refreshed["record"]["meta"]["projection_key"] == "status-card:infrascope-overview"
    assert payload["dispatcher"]["lifecycle"][0]["status"] == "ready"
