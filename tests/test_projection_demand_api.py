from __future__ import annotations

import sys
import types

from fastapi import FastAPI
from fastapi.testclient import TestClient

from adaos.apps.api.auth import require_token
from adaos.services.projection_demand import clear_projection_demand_registry


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
    app = FastAPI()
    app.include_router(node_api.router, prefix="/api/node")
    app.dependency_overrides[require_token] = lambda: None
    return TestClient(app)


def test_projection_demand_api_accepts_full_client_snapshot() -> None:
    client = _make_client()

    resp = client.post(
        "/api/node/projection-demand/client",
        json={
            "client_id": "browser-1",
            "device_id": "desktop",
            "session_id": "session-1",
            "webspace_id": "desktop",
            "role": "operator",
            "updated_at": 10.0,
            "subscriptions": [
                {
                    "projection_key": "status-card:runtime",
                    "consumer_id": "widget:runtime",
                    "consumer_kind": "widget",
                    "node_scope": {"node_id": "node-a"},
                    "pinned": True,
                    "visibility": "visible",
                }
            ],
        },
    )

    assert resp.status_code == 200
    payload = resp.json()
    assert payload["ok"] is True
    assert payload["webspace_id"] == "desktop"
    assert payload["record"]["client_id"] == "browser-1"
    assert payload["snapshot"]["projection_total"] == 1
    assert payload["snapshot"]["projections"][0]["projection_key"] == "status-card:runtime"


def test_projection_demand_api_get_and_delete_snapshot() -> None:
    client = _make_client()
    client.post(
        "/api/node/projection-demand/client",
        json={
            "client_id": "browser-1",
            "device_id": "desktop",
            "session_id": "session-1",
            "webspace_id": "desktop",
            "role": "operator",
            "subscriptions": [
                {
                    "projection_key": "projection:hub/overview",
                    "consumer_id": "page:infrascope",
                    "consumer_kind": "page",
                }
            ],
        },
    )

    snapshot_resp = client.get("/api/node/projection-demand", params={"webspace_id": "desktop"})
    delete_resp = client.delete(
        "/api/node/projection-demand/client/browser-1/session-1",
        params={"webspace_id": "desktop"},
    )

    assert snapshot_resp.status_code == 200
    assert snapshot_resp.json()["consumer_total"] == 1
    assert delete_resp.status_code == 200
    assert delete_resp.json()["deleted"] is True
    assert delete_resp.json()["snapshot"]["consumer_total"] == 0
