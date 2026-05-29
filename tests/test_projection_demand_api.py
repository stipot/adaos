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


def test_projection_demand_contract_endpoint_exposes_client_subscription_abi() -> None:
    client = _make_client()

    resp = client.get("/api/node/projection-demand/contract")

    assert resp.status_code == 200
    payload = resp.json()
    assert payload["contract"] == "adaos.client-projection-subscription.v1"
    assert payload["ready_for_mvp"] is True
    assert payload["registry"]["snapshot_endpoint"] == "/api/node/projection-demand"
    assert payload["write_policy"]["touch_extends_session_without_replacing_demand"] is True
    assert "projection:hub/object-inspector" in payload["sample_projection_keys"]


def test_projection_demand_surface_lifecycle_contract_endpoint_exposes_mapping() -> None:
    client = _make_client()

    resp = client.get("/api/node/projection-demand/surface-lifecycle-contract")

    assert resp.status_code == 200
    payload = resp.json()
    assert payload["contract"] == "adaos.browser-surface-lifecycle-subscriptions.v1"
    assert payload["ready_for_mvp"] is True
    assert payload["server_endpoint"] == "/api/node/projection-demand/browser-state"
    assert payload["output_contract"] == "adaos.client-projection-subscription.v1"
    assert payload["sample_subscription_total"] == 5
    assert "pinned-panel" in payload["sample_consumer_kinds"]


def test_projection_demand_restore_contract_endpoint_exposes_startup_rules() -> None:
    client = _make_client()

    resp = client.get("/api/node/projection-demand/restore-contract")

    assert resp.status_code == 200
    payload = resp.json()
    assert payload["contract"] == "adaos.projection-demand.restore-from-yjs.v1"
    assert payload["ready_for_mvp"] is True
    assert payload["status"] == "implemented"
    assert payload["runtime_helpers"]["projection_runtime"] == "ProjectionRuntime.restore_active_demand"
    assert payload["restore_modes"][1]["active_state"] == "active_receivers"
    assert payload["boundaries"]["restore_writes_yjs_directly"] is False
    assert payload["source_of_truth"]["yjs_path"] == "runtime/clients"


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


def test_projection_demand_api_keeps_webspace_snapshots_isolated() -> None:
    client = _make_client()
    for webspace_id in ["desktop", "dev"]:
        client.post(
            "/api/node/projection-demand/client",
            json={
                "client_id": f"browser-{webspace_id}",
                "device_id": "desktop",
                "session_id": "session-1",
                "webspace_id": webspace_id,
                "role": "operator",
                "subscriptions": [
                    {
                        "projection_key": "status-card:runtime",
                        "consumer_id": f"widget:{webspace_id}",
                        "consumer_kind": "widget",
                    }
                ],
            },
        )

    desktop_snapshot = client.get("/api/node/projection-demand", params={"webspace_id": "desktop"}).json()
    dev_snapshot = client.get("/api/node/projection-demand", params={"webspace_id": "dev"}).json()
    delete_resp = client.delete(
        "/api/node/projection-demand/client/browser-desktop/session-1",
        params={"webspace_id": "desktop"},
    )
    dev_after_delete = client.get("/api/node/projection-demand", params={"webspace_id": "dev"}).json()

    assert desktop_snapshot["webspace_id"] == "desktop"
    assert desktop_snapshot["consumer_total"] == 1
    assert desktop_snapshot["records"][0]["webspace_id"] == "desktop"
    assert dev_snapshot["webspace_id"] == "dev"
    assert dev_snapshot["consumer_total"] == 1
    assert dev_snapshot["records"][0]["webspace_id"] == "dev"
    assert delete_resp.json()["snapshot"]["consumer_total"] == 0
    assert dev_after_delete["consumer_total"] == 1
    assert dev_after_delete["projections"][0]["consumers"][0]["consumer_id"] == "widget:dev"


def test_projection_demand_api_touches_session_without_replacing_subscriptions() -> None:
    client = _make_client()
    client.post(
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
                    "consumer_id": "pinned:runtime",
                    "consumer_kind": "pinned-panel",
                    "pinned": True,
                }
            ],
        },
    )

    resp = client.post(
        "/api/node/projection-demand/client/browser-1/session-1/touch",
        params={"webspace_id": "desktop"},
        json={"updated_at": 19.0},
    )
    snapshot_resp = client.get(
        "/api/node/projection-demand",
        params={"webspace_id": "desktop", "stale_after_s": 5.0},
    )

    assert resp.status_code == 200
    payload = resp.json()
    assert payload["accepted"] is True
    assert payload["record"]["updated_at"] == 19.0
    assert payload["record"]["subscriptions"][0]["consumer_id"] == "pinned:runtime"
    assert payload["snapshot"]["projection_total"] == 1
    assert snapshot_resp.json()["records"][0]["updated_at"] == 19.0


def test_projection_demand_api_touch_reports_missing_session() -> None:
    client = _make_client()

    resp = client.post(
        "/api/node/projection-demand/client/browser-1/session-1/touch",
        params={"webspace_id": "desktop"},
        json={"updated_at": 19.0},
    )

    assert resp.status_code == 404
    assert resp.json()["detail"] == "projection_demand_session_not_found"


def test_projection_demand_api_accepts_browser_state_mapping() -> None:
    client = _make_client()

    resp = client.post(
        "/api/node/projection-demand/browser-state",
        json={
            "client_id": "browser-1",
            "device_id": "desktop",
            "session_id": "session-1",
            "webspace_id": "desktop",
            "role": "operator",
            "updated_at": 10.0,
            "page": {
                "id": "infrascope",
                "projectionKeys": ["projection:hub/overview"],
            },
            "widgets": [
                {
                    "id": "infra-state",
                    "projection_key": "status-card:runtime",
                }
            ],
            "modals": [
                {
                    "id": "runtime-details",
                    "projection_key": "projection:hub/object-inspector",
                    "visible": False,
                }
            ],
            "pinnedPanels": [
                {
                    "id": "runtime",
                    "projection_key": "status-card:runtime",
                }
            ],
        },
    )

    assert resp.status_code == 200
    payload = resp.json()
    assert payload["record"]["subscriptions"][0]["consumer_id"] == "page:infrascope"
    assert payload["record"]["subscriptions"][1]["consumer_id"] == "widget:infra-state"
    assert payload["record"]["subscriptions"][2]["visibility"] == "hidden"
    assert payload["record"]["subscriptions"][3]["consumer_kind"] == "pinned-panel"
    assert payload["record"]["subscriptions"][3]["pinned"] is True
    assert payload["snapshot"]["projection_total"] == 3
    assert payload["snapshot"]["consumer_total"] == 4


def test_projection_demand_api_materializes_yjs_cache(monkeypatch) -> None:
    client = _make_client()
    from adaos.apps.api import node_api

    captured = {}

    async def fake_materialize_projection_demand_to_yjs(**kwargs):
        captured.update(kwargs)
        return {
            "ok": True,
            "accepted": True,
            "webspace_id": kwargs["webspace_id"],
            "yjs_path": "runtime/clients",
            "client_total": 0,
        }

    monkeypatch.setattr(
        node_api,
        "materialize_projection_demand_to_yjs",
        fake_materialize_projection_demand_to_yjs,
    )

    resp = client.post(
        "/api/node/projection-demand/yjs/materialize",
        json={"webspace_id": "desktop", "now": 20.0},
    )

    assert resp.status_code == 200
    assert resp.json()["yjs_path"] == "runtime/clients"
    assert captured["webspace_id"] == "desktop"
    assert captured["now"] == 20.0


def test_projection_demand_api_restores_yjs_cache(monkeypatch) -> None:
    client = _make_client()
    from adaos.apps.api import node_api

    captured = {}

    async def fake_restore_projection_demand_from_yjs(**kwargs):
        captured.update(kwargs)
        return {
            "ok": True,
            "accepted": True,
            "webspace_id": kwargs["webspace_id"],
            "restored_total": 1,
            "skipped_total": 0,
        }

    monkeypatch.setattr(
        node_api,
        "restore_projection_demand_from_yjs",
        fake_restore_projection_demand_from_yjs,
    )

    resp = client.post(
        "/api/node/projection-demand/yjs/restore",
        json={"webspace_id": "desktop", "include_hidden": False, "stale_after_s": 5.0},
    )

    assert resp.status_code == 200
    assert resp.json()["restored_total"] == 1
    assert captured["webspace_id"] == "desktop"
    assert captured["include_hidden"] is False
    assert captured["stale_after_s"] == 5.0
