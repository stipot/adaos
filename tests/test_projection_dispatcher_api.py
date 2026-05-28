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
    app = FastAPI()
    app.include_router(node_api.router, prefix="/api/node")
    app.dependency_overrides[require_token] = lambda: None
    return TestClient(app)


def test_projection_dispatcher_snapshot_endpoint_is_empty_by_default() -> None:
    client = _make_client()

    resp = client.get("/api/node/projection-dispatcher")

    assert resp.status_code == 200
    payload = resp.json()
    assert payload["ok"] is True
    assert payload["handler_total"] == 1
    assert payload["handlers"] == ["status-card:*"]
    assert payload["stats"]["incoming_total"] == 0


def test_event_envelope_contract_endpoint_exposes_shared_abi() -> None:
    client = _make_client()

    resp = client.get("/api/node/event-envelope-contract")

    assert resp.status_code == 200
    payload = resp.json()
    assert payload["contract"] == "adaos.operational-event-envelope.v1"
    assert payload["ready_for_mvp"] is True
    assert payload["meta_path"] == "_meta.event"
    assert "scope" in payload["metadata_fields"]
    assert payload["normalized_example"]["event_id"] == "evt-demo-1"
    assert payload["dispatcher_ready"] is True


def test_projection_runtime_ownership_endpoint_exposes_split() -> None:
    client = _make_client()

    resp = client.get("/api/node/projection-runtime-ownership")

    assert resp.status_code == 200
    payload = resp.json()
    assert payload["contract"] == "adaos.projection-runtime-ownership.v1"
    assert payload["ready_for_mvp"] is True
    assert payload["boundary_total"] == 5
    assert payload["forbidden_total"] >= 5
    areas = {item["area"]: item for item in payload["boundaries"]}
    assert "select demanded projections" in areas["refresh_dispatch"]["core_owned"]
    assert "write full active subscription set" in areas["browser_demand"]["browser_owned"]
    assert "refresh payload for owned projection keys" in areas["refresh_dispatch"]["skill_owned"]
    assert "browser writes to data/projectionRecords" in areas["browser_demand"]["forbidden"]
    assert "/api/node/projection-dispatcher/core-skill-contract" in payload["evidence"]


def test_projection_dispatcher_core_skill_contract_endpoint_reports_demand() -> None:
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

    resp = client.get(
        "/api/node/projection-dispatcher/core-skill-contract",
        params={"webspace_id": "desktop"},
    )

    assert resp.status_code == 200
    payload = resp.json()
    assert payload["contract"] == "adaos.core-skill-projection-refresh.v1"
    assert payload["demand_total"] == 1
    assert payload["covered_total"] == 1
    assert payload["uncovered_total"] == 0
    assert payload["readiness"]["ready_for_dispatch"] is True
    assert payload["readiness"]["coverage_ratio"] == 1.0
    assert payload["readiness"]["status"] == "pass"
    assert payload["demands"][0]["projection_key"] == "status-card:runtime"
    assert payload["demands"][0]["handler"]["covered"] is True
    assert "projection demand selection" in payload["demands"][0]["ownership"]["core_owned"]
    assert "payload refresh" in payload["demands"][0]["ownership"]["skill_owned"]
    assert "active subscription set" in payload["demands"][0]["ownership"]["browser_owned"]
    assert payload["demands"][0]["refresh_contract"]["core_selects_demand"] is True
    assert payload["demands"][0]["refresh_contract"]["core_materializes_projection_record"] is True


def test_projection_dispatcher_memory_contract_endpoint_exposes_publication_boundary() -> None:
    client = _make_client()

    resp = client.get("/api/node/projection-dispatcher/memory-contract")

    assert resp.status_code == 200
    payload = resp.json()
    assert payload["contract"] == "adaos.projection-dispatcher.memory-vs-yjs.v1"
    assert payload["ready_for_mvp"] is True
    assert payload["yjs_publication"]["path"] == "data/projectionRecords"
    assert payload["dispatcher_boundaries"]["core_materializes_record"] is True
    assert payload["dispatcher_boundaries"]["handler_writes_yjs_directly"] is False


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


def test_projection_dispatcher_refreshes_demanded_status_card(monkeypatch) -> None:
    client = _make_client()
    registry = StatusRegistry()
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
    monkeypatch.setattr("adaos.services.status_projection.get_ctx", lambda: SimpleNamespace(status_registry=registry))
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
    refreshed = payload["report"]["refreshed"][0]
    assert payload["report"]["selected"][0]["projection_key"] == "status-card:runtime"
    assert refreshed["status"] == "ready"
    assert refreshed["reason"] == "materialized"
    assert refreshed["record"]["data"]["summary"] == "Runtime ready"
    assert refreshed["record"]["meta"]["projection_key"] == "status-card:runtime"
    assert payload["dispatcher"]["lifecycle"][0]["status"] == "ready"
