from __future__ import annotations

import sys
import types

from fastapi import FastAPI
from fastapi.testclient import TestClient

from adaos.apps.api.auth import require_token
from adaos.domain import make_client_subscription_record, make_projection_subscription
from adaos.services.projection_demand import clear_projection_demand_registry, write_client_subscription_record
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
    clear_projection_demand_registry()
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


def test_projection_records_api_materializes_yjs_cache(monkeypatch) -> None:
    client = _make_client()
    from adaos.apps.api import node_api

    captured = {}

    async def fake_materialize_projection_records_to_yjs(**kwargs):
        captured.update(kwargs)
        return {
            "ok": True,
            "accepted": True,
            "webspace_id": kwargs["webspace_id"],
            "yjs_path": "data/projectionRecords",
            "record_total": 1,
            "projection_keys": list(kwargs["projection_keys"] or []),
            "demanded_only": bool(kwargs["demanded_only"]),
        }

    monkeypatch.setattr(
        node_api,
        "materialize_projection_records_to_yjs",
        fake_materialize_projection_records_to_yjs,
    )

    resp = client.post(
        "/api/node/projection-records/yjs/materialize",
        json={
            "webspace_id": "desktop",
            "projection_keys": ["status-card:runtime"],
            "demanded_only": True,
        },
    )

    assert resp.status_code == 200
    payload = resp.json()
    assert payload["yjs_path"] == "data/projectionRecords"
    assert payload["projection_keys"] == ["status-card:runtime"]
    assert captured["webspace_id"] == "desktop"
    assert captured["projection_keys"] == ["status-card:runtime"]
    assert captured["demanded_only"] is True


def test_projection_records_api_reads_yjs_cache(monkeypatch) -> None:
    client = _make_client()
    from adaos.apps.api import node_api

    captured = {}

    async def fake_read_projection_records_yjs_cache(**kwargs):
        captured.update(kwargs)
        return {
            "ok": True,
            "accepted": True,
            "webspace_id": kwargs["webspace_id"],
            "cache_present": True,
            "yjs_path": "data/projectionRecords",
            "record_total": 1,
            "projection_keys": ["status-card:runtime"],
        }

    monkeypatch.setattr(
        node_api,
        "read_projection_records_yjs_cache",
        fake_read_projection_records_yjs_cache,
    )

    resp = client.get(
        "/api/node/projection-records/yjs/cache",
        params={"webspace_id": "desktop"},
    )

    assert resp.status_code == 200
    payload = resp.json()
    assert payload["cache_present"] is True
    assert payload["yjs_path"] == "data/projectionRecords"
    assert payload["projection_keys"] == ["status-card:runtime"]
    assert captured["webspace_id"] == "desktop"


def test_projection_records_api_exposes_browser_cache_snapshot() -> None:
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
        "/api/node/projection-records/browser-cache",
        params={"webspace_id": "desktop"},
    )

    assert write_resp.status_code == 200
    assert resp.status_code == 200
    payload = resp.json()
    assert payload["kind"] == "browser-demanded-projection-records"
    assert payload["demanded_only"] is True
    assert payload["record_total"] == 1
    assert payload["missing_record_total"] == 0
    assert payload["projection_keys"] == ["status-card:runtime"]
    assert payload["records"]["status-card:runtime"]["data"]["summary"] == "Runtime ready"
    assert payload["cache_contract"]["write_policy"] == "core-owned-cache-only"


def test_projection_records_api_filters_browser_cache_by_client_session() -> None:
    client = _make_client()
    for projection_key in ["status-card:runtime", "status-card:desktop-shell"]:
        client.post(
            "/api/node/projection-records",
            json={
                "status": "ready",
                "data": {"summary": projection_key},
                "meta": {
                    "projection_key": projection_key,
                    "kind": "status-card",
                    "webspace_id": "desktop",
                },
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
    write_client_subscription_record(
        make_client_subscription_record(
            client_id="browser-2",
            device_id="desktop",
            session_id="session-2",
            webspace_id="desktop",
            role="operator",
            subscriptions=[
                make_projection_subscription(
                    projection_key="status-card:desktop-shell",
                    consumer_id="widget:desktop-shell",
                    consumer_kind="widget",
                )
            ],
        )
    )

    resp = client.get(
        "/api/node/projection-records/browser-cache",
        params={"webspace_id": "desktop", "client_id": "browser-1", "session_id": "session-1"},
    )

    assert resp.status_code == 200
    payload = resp.json()
    assert payload["session_scoped"] is True
    assert payload["client_id"] == "browser-1"
    assert payload["session_id"] == "session-1"
    assert payload["projection_keys"] == ["status-card:runtime"]
    assert set(payload["records"]) == {"status-card:runtime"}


def test_projection_records_api_filters_browser_cache_by_projection_keys() -> None:
    client = _make_client()
    for projection_key in ["status-card:runtime", "status-card:desktop-shell"]:
        client.post(
            "/api/node/projection-records",
            json={
                "status": "ready",
                "data": {"summary": projection_key},
                "meta": {
                    "projection_key": projection_key,
                    "kind": "status-card",
                    "webspace_id": "desktop",
                },
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
                ),
                make_projection_subscription(
                    projection_key="status-card:desktop-shell",
                    consumer_id="widget:desktop-shell",
                    consumer_kind="widget",
                ),
            ],
        )
    )

    resp = client.get(
        "/api/node/projection-records/browser-cache",
        params={
            "webspace_id": "desktop",
            "projection_keys": ["status-card:desktop-shell"],
        },
    )

    assert resp.status_code == 200
    payload = resp.json()
    assert payload["projection_scoped"] is True
    assert payload["requested_projection_keys"] == ["status-card:desktop-shell"]
    assert payload["projection_keys"] == ["status-card:desktop-shell"]
    assert set(payload["records"]) == {"status-card:desktop-shell"}
