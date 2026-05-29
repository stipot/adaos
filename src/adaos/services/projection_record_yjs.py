from __future__ import annotations

import json
import time
from typing import Any, Iterable, Mapping

from adaos.domain import ProjectionRecord, normalize_projection_record, projection_fingerprint
from adaos.services.projection_demand import demanded_projection_keys
from adaos.services.projection_records import list_projection_records, projection_record_registry_snapshot
PROJECTION_RECORDS_YJS_KEY = "projectionRecords"
PROJECTION_RECORDS_YJS_PATH = f"data/{PROJECTION_RECORDS_YJS_KEY}"
PROJECTION_RECORDS_YJS_SCHEMA = "adaos.projection-records.v1"
PROJECTION_RECORDS_YJS_ENVELOPE_SCHEMA = "adaos.projection-records.envelope.v1"
PROJECTION_RECORDS_NODE_MULTIPLICITY_CONTRACT = "adaos.projection-records.node-multiplicity.v1"
PROJECTION_RECORDS_YJS_OWNER = "core:projection_records"
PROJECTION_RECORDS_YJS_WRITE_POLICY = "core-owned-cache-only"


def async_get_ydoc(*args: Any, **kwargs: Any) -> Any:
    from adaos.services.yjs.doc import async_get_ydoc as _async_get_ydoc

    return _async_get_ydoc(*args, **kwargs)


def async_read_ydoc(*args: Any, **kwargs: Any) -> Any:
    from adaos.services.yjs.doc import async_read_ydoc as _async_read_ydoc

    return _async_read_ydoc(*args, **kwargs)


def mutate_live_room(*args: Any, **kwargs: Any) -> Any:
    from adaos.services.yjs.doc import mutate_live_room as _mutate_live_room

    return _mutate_live_room(*args, **kwargs)


def _json_clone(value: Any) -> Any:
    return json.loads(json.dumps(value, ensure_ascii=False, sort_keys=True, default=str))


def _webspace_token(value: Any = None) -> str:
    token = str(value or "").strip()
    if token:
        return token
    try:
        from adaos.services.yjs.webspace import default_webspace_id

        return default_webspace_id()
    except Exception:
        return "default"


def _projection_key_set(values: Iterable[Any] | None) -> set[str] | None:
    if values is None:
        return None
    keys = {str(value or "").strip() for value in values if str(value or "").strip()}
    return keys


def _select_records(
    *,
    webspace_id: str,
    projection_keys: Iterable[Any] | None = None,
    demanded_only: bool = False,
) -> list[ProjectionRecord]:
    requested = _projection_key_set(projection_keys)
    if demanded_only:
        demanded = set(demanded_projection_keys(webspace_id=webspace_id))
        requested = demanded if requested is None else requested.intersection(demanded)
    records = list_projection_records(webspace_id=webspace_id)
    if requested is not None:
        records = [record for record in records if record.meta.projection_key in requested]
    return records


def _node_ids_from_records(records: Iterable[ProjectionRecord | Mapping[str, Any]]) -> list[str]:
    node_ids: set[str] = set()
    for item in records:
        if isinstance(item, ProjectionRecord):
            token = str(item.meta.node_id or "").strip()
        else:
            meta = item.get("meta") if isinstance(item.get("meta"), Mapping) else {}
            token = str(meta.get("node_id") or "").strip()
        if token:
            node_ids.add(token)
    return sorted(node_ids)


def _node_scoped_record_total(records: Iterable[ProjectionRecord | Mapping[str, Any]]) -> int:
    total = 0
    for item in records:
        if isinstance(item, ProjectionRecord):
            token = str(item.meta.node_id or "").strip()
        else:
            meta = item.get("meta") if isinstance(item.get("meta"), Mapping) else {}
            token = str(meta.get("node_id") or "").strip()
        if token:
            total += 1
    return total


def _projection_records_yjs_envelope(
    *,
    webspace_id: str,
    record_total: int,
    node_ids: Iterable[Any],
    node_scoped_record_total: int,
) -> dict[str, Any]:
    normalized_node_ids = sorted({str(item or "").strip() for item in node_ids if str(item or "").strip()})
    return {
        "schema": PROJECTION_RECORDS_YJS_ENVELOPE_SCHEMA,
        "owner": PROJECTION_RECORDS_YJS_OWNER,
        "write_policy": PROJECTION_RECORDS_YJS_WRITE_POLICY,
        "cache_role": "collaborative_projection_cache",
        "source_of_truth": "projection_record_registry",
        "webspace_id": webspace_id,
        "yjs_path": PROJECTION_RECORDS_YJS_PATH,
        "node_scope": {
            "mode": "record-meta-node-id",
            "node_ids": normalized_node_ids,
            "node_scoped_record_total": int(node_scoped_record_total),
            "record_total": int(record_total),
            "empty_scope_allowed": True,
        },
        "boundaries": {
            "skills_may_read": True,
            "skills_may_write": False,
            "browser_may_read": True,
            "browser_may_write": False,
        },
    }


def _projection_records_yjs_envelope_ok(
    envelope: Mapping[str, Any],
    *,
    expected: Mapping[str, Any],
) -> bool:
    node_scope = envelope.get("node_scope") if isinstance(envelope.get("node_scope"), Mapping) else {}
    expected_node_scope = (
        expected.get("node_scope") if isinstance(expected.get("node_scope"), Mapping) else {}
    )
    return (
        envelope.get("schema") == expected.get("schema")
        and envelope.get("owner") == expected.get("owner")
        and envelope.get("write_policy") == expected.get("write_policy")
        and envelope.get("webspace_id") == expected.get("webspace_id")
        and envelope.get("yjs_path") == expected.get("yjs_path")
        and node_scope.get("mode") == expected_node_scope.get("mode")
        and list(node_scope.get("node_ids") or []) == list(expected_node_scope.get("node_ids") or [])
        and int(node_scope.get("node_scoped_record_total") or 0)
        == int(expected_node_scope.get("node_scoped_record_total") or 0)
        and int(node_scope.get("record_total") or 0) == int(expected_node_scope.get("record_total") or 0)
    )


def build_projection_records_yjs_payload(
    *,
    webspace_id: str | None = None,
    projection_keys: Iterable[Any] | None = None,
    demanded_only: bool = False,
    now: float | None = None,
) -> dict[str, Any]:
    target_webspace_id = _webspace_token(webspace_id)
    ts = float(now if now is not None else time.time())
    records = _select_records(
        webspace_id=target_webspace_id,
        projection_keys=projection_keys,
        demanded_only=demanded_only,
    )
    items = [record.to_dict() for record in records]
    by_key = {str(record.meta.projection_key): record.to_dict() for record in records}
    node_scoped_record_total = _node_scoped_record_total(records)
    node_ids = _node_ids_from_records(records)
    registry = projection_record_registry_snapshot(webspace_id=target_webspace_id)
    payload = {
        "schema": PROJECTION_RECORDS_YJS_SCHEMA,
        "webspace_id": target_webspace_id,
        "yjs_path": PROJECTION_RECORDS_YJS_PATH,
        "registry_version": registry.get("registry_version"),
        "record_total": len(items),
        "node_scoped_record_total": node_scoped_record_total,
        "ready_total": sum(1 for record in records if record.status == "ready"),
        "stale_total": sum(1 for record in records if record.status == "stale"),
        "error_total": sum(1 for record in records if record.status == "error"),
        "unavailable_total": sum(1 for record in records if record.status == "unavailable"),
        "demanded_only": bool(demanded_only),
        "projection_keys": sorted(by_key),
        "node_ids": node_ids,
        "records": by_key,
        "items": items,
        "updated_at": ts,
    }
    payload["envelope"] = _projection_records_yjs_envelope(
        webspace_id=target_webspace_id,
        record_total=len(items),
        node_ids=node_ids,
        node_scoped_record_total=node_scoped_record_total,
    )
    payload["fingerprint"] = projection_fingerprint(
        {
            "schema": payload["schema"],
            "webspace_id": payload["webspace_id"],
            "registry_version": payload["registry_version"],
            "envelope": payload["envelope"],
            "records": by_key,
        }
    )
    return payload


def _write_payload_to_doc(ydoc: Any, txn: Any, payload: Mapping[str, Any]) -> bool:
    data_map = ydoc.get_map("data")
    current = data_map.get(PROJECTION_RECORDS_YJS_KEY)
    if isinstance(current, Mapping) and current.get("fingerprint") == payload.get("fingerprint"):
        return False
    data_map.set(txn, PROJECTION_RECORDS_YJS_KEY, _json_clone(dict(payload)))
    return True


def _cache_payload_summary(payload: Mapping[str, Any], *, webspace_id: str) -> dict[str, Any]:
    records = payload.get("records") if isinstance(payload.get("records"), Mapping) else {}
    projection_keys = payload.get("projection_keys")
    if not isinstance(projection_keys, list):
        projection_keys = sorted(str(key) for key in records)
    node_ids = payload.get("node_ids")
    if not isinstance(node_ids, list):
        normalized_records = [dict(record) for record in records.values()]
        node_ids = _node_ids_from_records(normalized_records)
    else:
        normalized_records = [dict(record) for record in records.values()]
    node_scoped_record_total = payload.get("node_scoped_record_total")
    if not isinstance(node_scoped_record_total, int):
        node_scoped_record_total = _node_scoped_record_total(normalized_records)
    expected_envelope = _projection_records_yjs_envelope(
        webspace_id=str(payload.get("webspace_id") or webspace_id),
        record_total=int(payload.get("record_total") or len(records)),
        node_ids=node_ids,
        node_scoped_record_total=int(node_scoped_record_total or 0),
    )
    envelope = payload.get("envelope") if isinstance(payload.get("envelope"), Mapping) else None
    fingerprint_source = {
        "schema": payload.get("schema"),
        "webspace_id": payload.get("webspace_id"),
        "registry_version": payload.get("registry_version"),
        "records": dict(records),
    }
    if isinstance(envelope, Mapping):
        fingerprint_source["envelope"] = dict(envelope)
    expected_fingerprint = projection_fingerprint(
        fingerprint_source
    )
    fingerprint = str(payload.get("fingerprint") or "")
    envelope_present = isinstance(envelope, Mapping)
    return {
        "ok": True,
        "accepted": True,
        "webspace_id": webspace_id,
        "cache_present": True,
        "yjs_path": PROJECTION_RECORDS_YJS_PATH,
        "schema": payload.get("schema"),
        "schema_ok": payload.get("schema") == PROJECTION_RECORDS_YJS_SCHEMA,
        "record_total": int(payload.get("record_total") or len(records)),
        "node_scoped_record_total": int(node_scoped_record_total or 0),
        "projection_keys": list(projection_keys),
        "node_ids": list(node_ids),
        "envelope_present": envelope_present,
        "envelope_ok": (
            _projection_records_yjs_envelope_ok(envelope, expected=expected_envelope)
            if isinstance(envelope, Mapping)
            else False
        ),
        "envelope": _json_clone(dict(envelope)) if isinstance(envelope, Mapping) else None,
        "expected_envelope": expected_envelope,
        "registry_version": payload.get("registry_version"),
        "fingerprint": fingerprint or None,
        "fingerprint_ok": bool(fingerprint) and fingerprint == expected_fingerprint,
        "updated_at": payload.get("updated_at"),
        "payload": _json_clone(dict(payload)),
    }


async def read_projection_records_yjs_cache(*, webspace_id: str | None = None) -> dict[str, Any]:
    target_webspace_id = _webspace_token(webspace_id)
    try:
        async with async_read_ydoc(target_webspace_id) as ydoc:
            data_map = ydoc.get_map("data")
            payload = data_map.get(PROJECTION_RECORDS_YJS_KEY)
    except Exception as exc:
        return {
            "ok": False,
            "accepted": False,
            "webspace_id": target_webspace_id,
            "cache_present": False,
            "yjs_path": PROJECTION_RECORDS_YJS_PATH,
            "error": f"{type(exc).__name__}: {exc}",
        }
    if not isinstance(payload, Mapping):
        return {
            "ok": True,
            "accepted": True,
            "webspace_id": target_webspace_id,
            "cache_present": False,
            "yjs_path": PROJECTION_RECORDS_YJS_PATH,
            "schema": PROJECTION_RECORDS_YJS_SCHEMA,
            "record_total": 0,
            "node_scoped_record_total": 0,
            "projection_keys": [],
            "node_ids": [],
            "envelope_present": False,
            "envelope_ok": False,
            "envelope": None,
            "expected_envelope": _projection_records_yjs_envelope(
                webspace_id=target_webspace_id,
                record_total=0,
                node_ids=[],
                node_scoped_record_total=0,
            ),
            "payload": None,
        }
    return _cache_payload_summary(payload, webspace_id=target_webspace_id)


async def materialize_projection_records_to_yjs(
    *,
    webspace_id: str | None = None,
    projection_keys: Iterable[Any] | None = None,
    demanded_only: bool = False,
    now: float | None = None,
) -> dict[str, Any]:
    target_webspace_id = _webspace_token(webspace_id)
    payload = build_projection_records_yjs_payload(
        webspace_id=target_webspace_id,
        projection_keys=projection_keys,
        demanded_only=demanded_only,
        now=now,
    )
    changed = {"value": False}

    def _apply(ydoc: Any, txn: Any) -> None:
        changed["value"] = _write_payload_to_doc(ydoc, txn, payload)

    live_applied = mutate_live_room(
        target_webspace_id,
        _apply,
        root_names=["data"],
        source="projection_record_yjs",
        owner="core:projection_records",
        channel="core.projection_records.live_room",
    )
    if not live_applied:
        async with async_get_ydoc(
            target_webspace_id,
            publish_live_room=True,
            load_mark_roots=["data"],
            write_source="projection_record_yjs",
            write_owner="core:projection_records",
            write_channel="core.projection_records.async",
        ) as ydoc:
            with ydoc.begin_transaction() as txn:
                changed["value"] = _write_payload_to_doc(ydoc, txn, payload)

    return {
        "ok": True,
        "accepted": True,
        "webspace_id": target_webspace_id,
        "yjs_path": PROJECTION_RECORDS_YJS_PATH,
        "schema": PROJECTION_RECORDS_YJS_SCHEMA,
        "demanded_only": bool(demanded_only),
        "projection_keys": list(payload["projection_keys"]),
        "node_ids": list(payload["node_ids"]),
        "envelope_present": True,
        "envelope_ok": True,
        "envelope": payload["envelope"],
        "record_total": int(payload["record_total"]),
        "node_scoped_record_total": int(payload["node_scoped_record_total"]),
        "registry_version": payload["registry_version"],
        "fingerprint": payload["fingerprint"],
        "written": bool(changed["value"]),
        "live_room": bool(live_applied),
        "payload": payload,
        "updated_at": payload["updated_at"],
    }


def normalize_projection_record_keys(records: Iterable[Mapping[str, Any] | ProjectionRecord]) -> list[str]:
    keys: list[str] = []
    for item in records:
        try:
            record = normalize_projection_record(item)
        except Exception:
            continue
        token = str(record.meta.projection_key or "").strip()
        if token:
            keys.append(token)
    return sorted(set(keys))


def projection_records_node_multiplicity_contract_snapshot(*, now: float | None = None) -> dict[str, Any]:
    """Return the browser-facing node multiplicity contract for projection records."""

    sample_records = [
        {
            "status": "ready",
            "data": {"summary": "Runtime on node-a"},
            "meta": {
                "projection_key": "status-card:runtime",
                "webspace_id": "desktop",
                "node_id": "node-a",
                "kind": "status-card",
            },
        },
        {
            "status": "ready",
            "data": {"summary": "Runtime on node-b"},
            "meta": {
                "projection_key": "projection:node/node-b/status-card:runtime",
                "webspace_id": "desktop",
                "node_id": "node-b",
                "kind": "status-card",
            },
        },
    ]
    node_ids = _node_ids_from_records(sample_records)
    node_scoped_record_total = _node_scoped_record_total(sample_records)
    envelope = _projection_records_yjs_envelope(
        webspace_id="desktop",
        record_total=len(sample_records),
        node_ids=node_ids,
        node_scoped_record_total=node_scoped_record_total,
    )
    return {
        "contract": PROJECTION_RECORDS_NODE_MULTIPLICITY_CONTRACT,
        "ready_for_mvp": True,
        "updated_at": float(now if now is not None else 0.0),
        "yjs_path": PROJECTION_RECORDS_YJS_PATH,
        "cache_schema": PROJECTION_RECORDS_YJS_SCHEMA,
        "envelope_schema": PROJECTION_RECORDS_YJS_ENVELOPE_SCHEMA,
        "node_scope_mode": "record-meta-node-id",
        "browser_read_path": "/api/node/projection-records/browser-cache",
        "yjs_read_path": "/api/node/projection-records/yjs/cache",
        "materialize_path": "/api/node/projection-records/yjs/materialize",
        "node_multiplicity_fields": [
            "payload.node_ids",
            "payload.node_scoped_record_total",
            "payload.envelope.node_scope.node_ids",
            "records[*].meta.node_id",
        ],
        "browser_rules": {
            "read_records_by_projection_key": True,
            "read_node_scope_from_meta": True,
            "do_not_assume_single_anonymous_node": True,
            "browser_writes_projection_cache": False,
        },
        "sample_node_ids": node_ids,
        "sample_node_scoped_record_total": node_scoped_record_total,
        "sample_envelope": envelope,
        "sample_records": sample_records,
    }


__all__ = [
    "PROJECTION_RECORDS_YJS_KEY",
    "PROJECTION_RECORDS_YJS_PATH",
    "PROJECTION_RECORDS_YJS_ENVELOPE_SCHEMA",
    "PROJECTION_RECORDS_NODE_MULTIPLICITY_CONTRACT",
    "PROJECTION_RECORDS_YJS_OWNER",
    "PROJECTION_RECORDS_YJS_SCHEMA",
    "PROJECTION_RECORDS_YJS_WRITE_POLICY",
    "build_projection_records_yjs_payload",
    "materialize_projection_records_to_yjs",
    "normalize_projection_record_keys",
    "projection_records_node_multiplicity_contract_snapshot",
    "read_projection_records_yjs_cache",
]
