from __future__ import annotations

import asyncio

from adaos.domain import make_client_subscription_record, make_projection_subscription
from adaos.services.projection_demand import (
    clear_projection_demand_registry,
    projection_demand_snapshot,
    write_client_subscription_record,
)
from adaos.services.projection_demand_yjs import (
    PROJECTION_DEMAND_YJS_OWNER,
    PROJECTION_DEMAND_YJS_WRITE_POLICY,
    materialize_projection_demand_to_yjs,
    read_projection_demand_yjs,
    restore_projection_demand_from_yjs,
)


class _FakeMap(dict):
    def set(self, _txn: object, key: str, value: object) -> None:
        self[key] = value


class _FakeDoc:
    def __init__(self) -> None:
        self.maps = {"runtime": _FakeMap()}

    def get_map(self, name: str) -> _FakeMap:
        return self.maps.setdefault(name, _FakeMap())

    def begin_transaction(self) -> "_FakeDoc":
        return self

    def __enter__(self) -> object:
        return object()

    def __exit__(self, exc_type: object, exc: object, tb: object) -> bool:
        return False


class _FakeAsyncDocContext:
    def __init__(self, doc: _FakeDoc) -> None:
        self.doc = doc

    async def __aenter__(self) -> _FakeDoc:
        return self.doc

    async def __aexit__(self, exc_type: object, exc: object, tb: object) -> bool:
        return False


def setup_function() -> None:
    clear_projection_demand_registry()


def _write_demand() -> None:
    write_client_subscription_record(
        make_client_subscription_record(
            client_id="browser-1",
            device_id="desktop",
            session_id="session-1",
            webspace_id="desktop",
            role="operator",
            updated_at=10.0,
            subscriptions=[
                make_projection_subscription(
                    projection_key="status-card:runtime",
                    consumer_id="widget:runtime",
                    consumer_kind="widget",
                )
            ],
        )
    )


def test_materialize_projection_demand_to_yjs_writes_runtime_clients(monkeypatch) -> None:
    fake_doc = _FakeDoc()
    from adaos.services import projection_demand_yjs

    monkeypatch.setattr(projection_demand_yjs, "mutate_live_room", lambda *_args, **_kwargs: False)
    monkeypatch.setattr(projection_demand_yjs, "async_get_ydoc", lambda *_args, **_kwargs: _FakeAsyncDocContext(fake_doc))
    _write_demand()

    result = asyncio.run(materialize_projection_demand_to_yjs(webspace_id="desktop", now=20.0))

    runtime = fake_doc.get_map("runtime")
    payload = runtime["projectionDemand"]
    assert result["written"] is True
    assert result["yjs_path"] == "runtime/clients"
    assert result["client_total"] == 1
    assert result["projection_keys"] == ["status-card:runtime"]
    assert payload["envelope"]["owner"] == PROJECTION_DEMAND_YJS_OWNER
    assert payload["envelope"]["write_policy"] == PROJECTION_DEMAND_YJS_WRITE_POLICY
    assert runtime["clients"]["browser-1"]["session-1"]["subscriptions"][0]["projection_key"] == "status-card:runtime"


def test_restore_projection_demand_from_yjs_rebuilds_registry(monkeypatch) -> None:
    fake_doc = _FakeDoc()
    from adaos.services import projection_demand_yjs

    monkeypatch.setattr(projection_demand_yjs, "mutate_live_room", lambda *_args, **_kwargs: False)
    monkeypatch.setattr(projection_demand_yjs, "async_get_ydoc", lambda *_args, **_kwargs: _FakeAsyncDocContext(fake_doc))
    monkeypatch.setattr(projection_demand_yjs, "async_read_ydoc", lambda *_args, **_kwargs: _FakeAsyncDocContext(fake_doc))
    _write_demand()
    asyncio.run(materialize_projection_demand_to_yjs(webspace_id="desktop", now=20.0))
    clear_projection_demand_registry()

    result = asyncio.run(restore_projection_demand_from_yjs(webspace_id="desktop", now=30.0))
    snapshot = projection_demand_snapshot(webspace_id="desktop")

    assert result["restored_total"] == 1
    assert result["skipped_total"] == 0
    assert snapshot["consumer_total"] == 1
    assert snapshot["projections"][0]["projection_key"] == "status-card:runtime"


def test_read_projection_demand_yjs_returns_summary(monkeypatch) -> None:
    fake_doc = _FakeDoc()
    from adaos.services import projection_demand_yjs

    monkeypatch.setattr(projection_demand_yjs, "mutate_live_room", lambda *_args, **_kwargs: False)
    monkeypatch.setattr(projection_demand_yjs, "async_get_ydoc", lambda *_args, **_kwargs: _FakeAsyncDocContext(fake_doc))
    monkeypatch.setattr(projection_demand_yjs, "async_read_ydoc", lambda *_args, **_kwargs: _FakeAsyncDocContext(fake_doc))
    _write_demand()
    asyncio.run(materialize_projection_demand_to_yjs(webspace_id="desktop", now=20.0))

    result = asyncio.run(read_projection_demand_yjs(webspace_id="desktop"))

    assert result["cache_present"] is True
    assert result["schema_ok"] is True
    assert result["fingerprint_ok"] is True
    assert result["client_total"] == 1
    assert result["consumer_total"] == 1
