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


def test_projection_diagnostics_links_demand_handlers_and_status_cards() -> None:
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
                    pinned=True,
                ),
                make_projection_subscription(
                    projection_key="projection:hub/overview",
                    consumer_id="page:infrascope",
                    consumer_kind="page",
                ),
            ],
            updated_at=10.0,
        )
    )

    resp = client.get(
        "/api/node/projection-diagnostics",
        params={"webspace_id": "desktop", "include_runtime": "false"},
    )

    assert resp.status_code == 200
    payload = resp.json()
    assert payload["active_projection_total"] == 2
    assert payload["active_consumer_total"] == 2
    assert payload["missing_handler_total"] == 1
    assert payload["missing_status_card_total"] == 0
    by_key = {item["projection_key"]: item for item in payload["active_projections"]}
    runtime = by_key["status-card:runtime"]
    assert runtime["handler"] == {"available": True, "key": "status-card:*", "match": "wildcard"}
    assert runtime["status_card"]["published"] is True
    assert runtime["status_card"]["summary"] == "Runtime ready"
    assert runtime["status_card"]["projection_status"] == "ready"
    assert runtime["pinned_total"] == 1
    assert by_key["projection:hub/overview"]["handler"]["available"] is False


def test_projection_diagnostics_counts_missing_status_card_for_demand() -> None:
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
                    projection_key="status-card:infrastate-summary",
                    consumer_id="widget:infra-state",
                    consumer_kind="widget",
                )
            ],
            updated_at=10.0,
        )
    )

    resp = client.get(
        "/api/node/projection-diagnostics",
        params={"webspace_id": "desktop", "include_runtime": "false"},
    )

    assert resp.status_code == 200
    payload = resp.json()
    assert payload["active_projection_total"] == 1
    assert payload["missing_handler_total"] == 0
    assert payload["missing_status_card_total"] == 1
    projection = payload["active_projections"][0]
    assert projection["handler"]["key"] == "status-card:*"
    assert projection["status_card"] is None
