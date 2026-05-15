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
