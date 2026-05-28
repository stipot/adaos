from __future__ import annotations

import sys
import types
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient

from adaos.apps.api.auth import require_token
from adaos.domain import make_client_subscription_record, make_projection_subscription
from adaos.services.projection_demand import clear_projection_demand_registry, write_client_subscription_record
from adaos.services.projection_dispatcher import clear_projection_dispatcher
from adaos.services.projection_records import clear_projection_record_registry
from adaos.services.status import StatusRegistry


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
    clear_projection_record_registry()
    app = FastAPI()
    app.include_router(node_api.router, prefix="/api/node")
    app.dependency_overrides[require_token] = lambda: None
    return TestClient(app)


def _publish_runtime_card(registry: StatusRegistry) -> None:
    registry.publish(
        {
            "id": "runtime",
            "owner": "core:runtime",
            "kind": "runtime",
            "scope": "platform",
            "webspace_id": "desktop",
            "status": "ready",
            "summary": "Runtime ready",
            "updated_at": 10.0,
        }
    )


def _write_runtime_demand() -> None:
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


def test_projection_diagnostics_links_demand_handlers_and_status_cards(monkeypatch) -> None:
    client = _make_client()
    registry = StatusRegistry()
    _publish_runtime_card(registry)
    _write_runtime_demand()
    monkeypatch.setattr("adaos.services.status_projection.get_ctx", lambda: SimpleNamespace(status_registry=registry))

    resp = client.get("/api/node/projection-diagnostics", params={"webspace_id": "desktop"})

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
    assert runtime["projection_record"]["materialized"] is False
    assert runtime["pinned_total"] == 1
    assert by_key["projection:hub/overview"]["handler"]["available"] is False


def test_projection_diagnostics_can_materialize_demanded_status_cards(monkeypatch) -> None:
    client = _make_client()
    registry = StatusRegistry()
    _publish_runtime_card(registry)
    _write_runtime_demand()
    monkeypatch.setattr("adaos.services.status_projection.get_ctx", lambda: SimpleNamespace(status_registry=registry))

    resp = client.get(
        "/api/node/projection-diagnostics",
        params={"webspace_id": "desktop", "materialize_status_cards": "true"},
    )

    assert resp.status_code == 200
    payload = resp.json()
    assert payload["refreshes"]["status_cards"]["demanded_only"] is True
    assert payload["refreshes"]["status_cards"]["materialized_total"] == 1
    assert payload["materialized_projection_total"] == 1
    projection = {item["projection_key"]: item for item in payload["active_projections"]}["status-card:runtime"]
    assert projection["projection_record"]["materialized"] is True
    assert projection["projection_record"]["status"] == "ready"
    assert projection["projection_record"]["lifecycle_reason"] == "materialized"
    assert payload["projection_registry"]["record_total"] == 1


def test_projection_diagnostics_can_include_yjs_cache_readback(monkeypatch) -> None:
    client = _make_client()
    registry = StatusRegistry()
    _publish_runtime_card(registry)
    _write_runtime_demand()
    monkeypatch.setattr("adaos.services.status_projection.get_ctx", lambda: SimpleNamespace(status_registry=registry))

    async def fake_read_projection_records_yjs_cache(**_kwargs):
        return {
            "ok": True,
            "accepted": True,
            "webspace_id": "desktop",
            "cache_present": True,
            "schema_ok": True,
            "fingerprint_ok": True,
            "node_ids": ["node-a"],
            "envelope_ok": True,
            "envelope": {"node_scope": {"node_ids": ["node-a"]}},
            "payload": {
                "records": {
                    "status-card:runtime": {
                        "status": "ready",
                        "data": {"summary": "Runtime ready"},
                        "meta": {
                            "projection_key": "status-card:runtime",
                            "kind": "status-card",
                            "webspace_id": "desktop",
                            "node_id": "node-a",
                            "version": 1,
                            "fingerprint": "fp-1",
                        },
                    }
                },
                "updated_at": 20.0,
            },
        }

    monkeypatch.setattr(
        "adaos.apps.api.node_api.read_projection_records_yjs_cache",
        fake_read_projection_records_yjs_cache,
    )

    resp = client.get(
        "/api/node/projection-diagnostics",
        params={"webspace_id": "desktop", "include_yjs_cache": "true"},
    )

    assert resp.status_code == 200
    payload = resp.json()
    assert payload["yjs_cache_checked"] is True
    assert payload["yjs_cache_projection_total"] == 1
    assert payload["yjs_cache_envelope_ok"] is True
    assert payload["yjs_cache_node_ids"] == ["node-a"]
    projection = {item["projection_key"]: item for item in payload["active_projections"]}["status-card:runtime"]
    assert projection["yjs_cache_record"]["cached"] is True
    assert projection["yjs_cache_record"]["status"] == "ready"
