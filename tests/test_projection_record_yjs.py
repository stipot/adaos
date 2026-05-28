from __future__ import annotations

import asyncio
from typing import Any

from adaos.domain import make_client_subscription_record, make_projection_record, make_projection_subscription
from adaos.services.projection_demand import clear_projection_demand_registry, write_client_subscription_record
from adaos.services.projection_records import clear_projection_record_registry, write_projection_record
from adaos.services.projection_record_yjs import (
    PROJECTION_RECORDS_YJS_ENVELOPE_SCHEMA,
    PROJECTION_RECORDS_YJS_OWNER,
    PROJECTION_RECORDS_YJS_WRITE_POLICY,
    materialize_projection_records_to_yjs,
    projection_records_node_multiplicity_contract_snapshot,
    read_projection_records_yjs_cache,
)


class _FakeMap(dict):
    def set(self, _txn: object, key: str, value: object) -> None:
        self[key] = value


class _FakeDoc:
    def __init__(self) -> None:
        self.maps = {"data": _FakeMap()}

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
    clear_projection_record_registry()
    clear_projection_demand_registry()


def _record(
    projection_key: str,
    *,
    webspace_id: str = "desktop",
    summary: str = "ok",
    node_id: str | None = None,
) -> dict[str, Any]:
    return make_projection_record(
        projection_key=projection_key,
        kind="status-card",
        webspace_id=webspace_id,
        node_id=node_id,
        data={"summary": summary},
        source="test",
        source_authority="test-suite",
        updated_at=10.0,
    ).to_dict()


def test_materialize_projection_records_to_yjs_writes_compact_cache(monkeypatch) -> None:
    fake_doc = _FakeDoc()
    from adaos.services import projection_record_yjs

    monkeypatch.setattr(projection_record_yjs, "mutate_live_room", lambda *_args, **_kwargs: False)
    monkeypatch.setattr(projection_record_yjs, "async_get_ydoc", lambda *_args, **_kwargs: _FakeAsyncDocContext(fake_doc))
    write_projection_record(_record("status-card:runtime", summary="Runtime ready"))

    result = asyncio.run(materialize_projection_records_to_yjs(webspace_id="desktop", now=20.0))

    payload = fake_doc.get_map("data")["projectionRecords"]
    assert result["written"] is True
    assert result["yjs_path"] == "data/projectionRecords"
    assert result["record_total"] == 1
    assert payload["schema"] == "adaos.projection-records.v1"
    assert payload["envelope"]["schema"] == PROJECTION_RECORDS_YJS_ENVELOPE_SCHEMA
    assert payload["envelope"]["owner"] == PROJECTION_RECORDS_YJS_OWNER
    assert payload["envelope"]["write_policy"] == PROJECTION_RECORDS_YJS_WRITE_POLICY
    assert payload["envelope"]["node_scope"]["mode"] == "record-meta-node-id"
    assert result["envelope_ok"] is True
    assert payload["records"]["status-card:runtime"]["data"]["summary"] == "Runtime ready"
    assert payload["projection_keys"] == ["status-card:runtime"]


def test_materialize_projection_records_to_yjs_can_filter_demanded_records(monkeypatch) -> None:
    fake_doc = _FakeDoc()
    from adaos.services import projection_record_yjs

    monkeypatch.setattr(projection_record_yjs, "mutate_live_room", lambda *_args, **_kwargs: False)
    monkeypatch.setattr(projection_record_yjs, "async_get_ydoc", lambda *_args, **_kwargs: _FakeAsyncDocContext(fake_doc))
    write_projection_record(_record("status-card:runtime", summary="Runtime ready"))
    write_projection_record(_record("status-card:desktop-shell", summary="Desktop ready"))
    write_client_subscription_record(
        make_client_subscription_record(
            client_id="browser-1",
            device_id="desktop",
            session_id="session-1",
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

    result = asyncio.run(materialize_projection_records_to_yjs(webspace_id="desktop", demanded_only=True, now=20.0))

    payload = fake_doc.get_map("data")["projectionRecords"]
    assert result["demanded_only"] is True
    assert result["record_total"] == 1
    assert result["projection_keys"] == ["status-card:desktop-shell"]
    assert set(payload["records"]) == {"status-card:desktop-shell"}


def test_projection_records_yjs_cache_preserves_node_scope(monkeypatch) -> None:
    fake_doc = _FakeDoc()
    from adaos.services import projection_record_yjs

    monkeypatch.setattr(projection_record_yjs, "mutate_live_room", lambda *_args, **_kwargs: False)
    monkeypatch.setattr(projection_record_yjs, "async_get_ydoc", lambda *_args, **_kwargs: _FakeAsyncDocContext(fake_doc))
    monkeypatch.setattr(projection_record_yjs, "async_read_ydoc", lambda *_args, **_kwargs: _FakeAsyncDocContext(fake_doc))
    write_projection_record(_record("status-card:runtime", summary="Runtime ready", node_id="node-a"))

    result = asyncio.run(materialize_projection_records_to_yjs(webspace_id="desktop", now=20.0))
    readback = asyncio.run(read_projection_records_yjs_cache(webspace_id="desktop"))

    payload = fake_doc.get_map("data")["projectionRecords"]
    assert result["node_scoped_record_total"] == 1
    assert result["node_ids"] == ["node-a"]
    assert payload["node_scoped_record_total"] == 1
    assert payload["node_ids"] == ["node-a"]
    assert payload["envelope"]["node_scope"]["node_ids"] == ["node-a"]
    assert payload["envelope"]["node_scope"]["node_scoped_record_total"] == 1
    assert payload["records"]["status-card:runtime"]["meta"]["node_id"] == "node-a"
    assert readback["node_scoped_record_total"] == 1
    assert readback["node_ids"] == ["node-a"]
    assert readback["envelope_present"] is True
    assert readback["envelope_ok"] is True
    assert readback["envelope"]["node_scope"]["node_ids"] == ["node-a"]
    assert readback["payload"]["records"]["status-card:runtime"]["meta"]["node_id"] == "node-a"


def test_materialize_projection_records_to_yjs_uses_live_room_when_available(monkeypatch) -> None:
    fake_doc = _FakeDoc()
    from adaos.services import projection_record_yjs

    def fake_mutate_live_room(_webspace_id: str, mutator, **_kwargs) -> bool:
        with fake_doc.begin_transaction() as txn:
            mutator(fake_doc, txn)
        return True

    def fail_async_get_ydoc(*_args, **_kwargs):
        raise AssertionError("async_get_ydoc should not be used when live room accepts the write")

    monkeypatch.setattr(projection_record_yjs, "mutate_live_room", fake_mutate_live_room)
    monkeypatch.setattr(projection_record_yjs, "async_get_ydoc", fail_async_get_ydoc)
    write_projection_record(_record("status-card:runtime", summary="Runtime ready"))

    result = asyncio.run(materialize_projection_records_to_yjs(webspace_id="desktop", now=20.0))

    assert result["live_room"] is True
    assert result["written"] is True
    assert fake_doc.get_map("data")["projectionRecords"]["record_total"] == 1


def test_read_projection_records_yjs_cache_returns_payload_summary(monkeypatch) -> None:
    fake_doc = _FakeDoc()
    from adaos.services import projection_record_yjs

    monkeypatch.setattr(projection_record_yjs, "mutate_live_room", lambda *_args, **_kwargs: False)
    monkeypatch.setattr(projection_record_yjs, "async_get_ydoc", lambda *_args, **_kwargs: _FakeAsyncDocContext(fake_doc))
    monkeypatch.setattr(projection_record_yjs, "async_read_ydoc", lambda *_args, **_kwargs: _FakeAsyncDocContext(fake_doc))
    write_projection_record(_record("status-card:runtime", summary="Runtime ready"))
    asyncio.run(materialize_projection_records_to_yjs(webspace_id="desktop", now=20.0))

    result = asyncio.run(read_projection_records_yjs_cache(webspace_id="desktop"))

    assert result["ok"] is True
    assert result["cache_present"] is True
    assert result["schema_ok"] is True
    assert result["fingerprint_ok"] is True
    assert result["record_total"] == 1
    assert result["projection_keys"] == ["status-card:runtime"]
    assert result["envelope_present"] is True
    assert result["envelope_ok"] is True
    assert result["envelope"]["owner"] == "core:projection_records"
    assert result["payload"]["records"]["status-card:runtime"]["data"]["summary"] == "Runtime ready"


def test_read_projection_records_yjs_cache_handles_missing_cache(monkeypatch) -> None:
    fake_doc = _FakeDoc()
    from adaos.services import projection_record_yjs

    monkeypatch.setattr(projection_record_yjs, "async_read_ydoc", lambda *_args, **_kwargs: _FakeAsyncDocContext(fake_doc))

    result = asyncio.run(read_projection_records_yjs_cache(webspace_id="desktop"))

    assert result["ok"] is True
    assert result["cache_present"] is False
    assert result["yjs_path"] == "data/projectionRecords"
    assert result["record_total"] == 0
    assert result["node_scoped_record_total"] == 0
    assert result["projection_keys"] == []
    assert result["envelope_present"] is False
    assert result["envelope_ok"] is False
    assert result["expected_envelope"]["node_scope"]["record_total"] == 0


def test_projection_records_node_multiplicity_contract_snapshot_exposes_browser_rules() -> None:
    snapshot = projection_records_node_multiplicity_contract_snapshot(now=60.0)

    assert snapshot["contract"] == "adaos.projection-records.node-multiplicity.v1"
    assert snapshot["ready_for_mvp"] is True
    assert snapshot["updated_at"] == 60.0
    assert snapshot["yjs_path"] == "data/projectionRecords"
    assert snapshot["node_scope_mode"] == "record-meta-node-id"
    assert snapshot["sample_node_ids"] == ["node-a", "node-b"]
    assert snapshot["sample_node_scoped_record_total"] == 2
    assert snapshot["sample_envelope"]["node_scope"]["node_ids"] == ["node-a", "node-b"]
    assert snapshot["browser_rules"]["do_not_assume_single_anonymous_node"] is True
    assert snapshot["browser_rules"]["browser_writes_projection_cache"] is False
