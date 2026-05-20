from __future__ import annotations

import sys
import types

from fastapi import FastAPI
from fastapi.testclient import TestClient

from adaos.apps.api.auth import require_token
from adaos.services.projection_records import clear_projection_record_registry


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

    clear_projection_record_registry()
    app = FastAPI()
    app.include_router(node_api.router, prefix="/api/node")
    app.dependency_overrides[require_token] = lambda: None
    return TestClient(app)


def test_projection_records_api_writes_and_reads_record() -> None:
    client = _make_client()

    write_resp = client.post(
        "/api/node/projection-records",
        json={
            "status": "ready",
            "data": {"summary": "Runtime ready"},
            "meta": {
                "projection_key": "status-card:runtime",
                "kind": "status-card",
                "webspace_id": "desktop",
                "version": 1,
                "fingerprint": "fp-1",
            },
        },
    )
    item_resp = client.get(
        "/api/node/projection-records/item",
        params={"webspace_id": "desktop", "projection_key": "status-card:runtime"},
    )

    assert write_resp.status_code == 200
    assert write_resp.json()["snapshot"]["record_total"] == 1
    assert item_resp.status_code == 200
    assert item_resp.json()["record"]["data"]["summary"] == "Runtime ready"


def test_projection_records_api_rejects_missing_identity() -> None:
    client = _make_client()

    resp = client.post(
        "/api/node/projection-records",
        json={"status": "ready", "data": {"summary": "Runtime ready"}, "meta": {}},
    )

    assert resp.status_code == 400
    assert resp.json()["detail"] == "webspace_id is required"
