from __future__ import annotations

import time
from threading import RLock
from typing import Any, Iterable, Mapping

from adaos.domain import ProjectionRecord, normalize_projection_record, projection_fingerprint
from adaos.services.projection_demand import projection_demand_consumers


_LOCK = RLock()
_RECORDS: dict[tuple[str, str], ProjectionRecord] = {}
_DEFAULT_STATS: dict[str, float | int | None] = {
    "registry_version": 0,
    "write_total": 0,
    "changed_total": 0,
    "unchanged_total": 0,
    "last_write_at": None,
    "last_write_latency_ms": None,
}
_STATS: dict[str, float | int | None] = dict(_DEFAULT_STATS)
PROJECTION_RECORD_BROWSER_ADAPTER_CONTRACT = "adaos.projection-records.browser-adapter.v1"


def _record_key(record: ProjectionRecord) -> tuple[str, str]:
    webspace_id = str(record.meta.webspace_id or "").strip()
    projection_key = str(record.meta.projection_key or "").strip()
    if not webspace_id:
        raise ValueError("webspace_id is required")
    if not projection_key:
        raise ValueError("projection_key is required")
    if not str(record.meta.kind or "").strip():
        raise ValueError("kind is required")
    return (webspace_id, projection_key)


def _content_fingerprint(record: ProjectionRecord) -> str:
    meta = record.meta
    return projection_fingerprint(
        {
            "status": record.status,
            "data": record.data,
            "error": record.error,
            "meta": {
                "projection_key": meta.projection_key,
                "kind": meta.kind,
                "webspace_id": meta.webspace_id,
                "node_id": meta.node_id,
                "fingerprint": meta.fingerprint,
                "source": meta.source,
                "source_authority": meta.source_authority,
                "access": meta.access,
                "lifecycle_reason": meta.lifecycle_reason,
            },
        }
    )


def clear_projection_record_registry() -> None:
    with _LOCK:
        _RECORDS.clear()
        _STATS.clear()
        _STATS.update(_DEFAULT_STATS)


def write_projection_record(record: Mapping[str, Any] | ProjectionRecord) -> ProjectionRecord:
    started_at = time.perf_counter()
    normalized = normalize_projection_record(record)
    key = _record_key(normalized)
    with _LOCK:
        previous = _RECORDS.get(key)
        _RECORDS[key] = normalized
        _STATS["write_total"] = int(_STATS.get("write_total") or 0) + 1
        if previous is not None and _content_fingerprint(previous) == _content_fingerprint(normalized):
            _STATS["unchanged_total"] = int(_STATS.get("unchanged_total") or 0) + 1
        else:
            _STATS["changed_total"] = int(_STATS.get("changed_total") or 0) + 1
            _STATS["registry_version"] = int(_STATS.get("registry_version") or 0) + 1
        _STATS["last_write_at"] = float(time.time())
        _STATS["last_write_latency_ms"] = round(max(0.0, time.perf_counter() - started_at) * 1000.0, 3)
        return normalized


def write_projection_record_if_valid(record: Mapping[str, Any] | ProjectionRecord | None) -> ProjectionRecord | None:
    if record is None:
        return None
    try:
        return write_projection_record(record)
    except (TypeError, ValueError, AttributeError):
        return None


def get_projection_record(*, webspace_id: str, projection_key: str) -> ProjectionRecord | None:
    key = (str(webspace_id or "").strip(), str(projection_key or "").strip())
    if not key[0] or not key[1]:
        return None
    with _LOCK:
        return _RECORDS.get(key)


def list_projection_records(*, webspace_id: str | None = None) -> list[ProjectionRecord]:
    webspace_token = str(webspace_id or "").strip()
    with _LOCK:
        records = list(_RECORDS.values())
    if webspace_token:
        records = [record for record in records if record.meta.webspace_id == webspace_token]
    return sorted(records, key=lambda item: (item.meta.webspace_id, item.meta.projection_key))


def projection_record_registry_snapshot(*, webspace_id: str | None = None) -> dict[str, Any]:
    records = list_projection_records(webspace_id=webspace_id)
    with _LOCK:
        stats = dict(_STATS)
    return {
        "ok": True,
        "webspace_id": str(webspace_id or "").strip() or None,
        "registry_version": int(stats.get("registry_version") or 0),
        "record_total": len(records),
        "ready_total": sum(1 for record in records if record.status == "ready"),
        "stale_total": sum(1 for record in records if record.status == "stale"),
        "error_total": sum(1 for record in records if record.status == "error"),
        "unavailable_total": sum(1 for record in records if record.status == "unavailable"),
        "stats": stats,
        "records": [record.to_dict() for record in records],
        "updated_at": time.time(),
    }


def _projection_key_filter(values: Iterable[Any] | None) -> set[str] | None:
    if values is None:
        return None
    keys = {str(value or "").strip() for value in values if str(value or "").strip()}
    return keys


def _browser_cache_key(
    *,
    webspace_id: str | None,
    client_id: str | None,
    session_id: str | None,
    projection_keys: Iterable[Any],
) -> str:
    key_scope = ",".join(str(item) for item in projection_keys) or "*"
    return ":".join(
        [
            "browser-projection-records",
            str(webspace_id or "*"),
            str(client_id or "*"),
            str(session_id or "*"),
            key_scope,
        ]
    )


def _browser_cache_entry_metadata(
    *,
    webspace_id: str | None,
    client_id: str | None,
    session_id: str | None,
    projection_key: str,
    record_payload: Mapping[str, Any] | None,
    cached: bool,
) -> dict[str, Any]:
    meta = record_payload.get("meta") if isinstance(record_payload, Mapping) else None
    meta_payload = meta if isinstance(meta, Mapping) else {}
    status = record_payload.get("status") if isinstance(record_payload, Mapping) else None
    record_version = meta_payload.get("version")
    record_fingerprint = meta_payload.get("fingerprint")
    fingerprint = projection_fingerprint(
        {
            "webspace_id": webspace_id,
            "client_id": client_id,
            "session_id": session_id,
            "projection_key": projection_key,
            "cached": bool(cached),
            "status": status,
            "record_version": record_version,
            "record_fingerprint": record_fingerprint,
        }
    )
    return {
        "key": _browser_cache_key(
            webspace_id=webspace_id,
            client_id=client_id,
            session_id=session_id,
            projection_keys=[projection_key],
        ),
        "fingerprint": fingerprint,
        "etag": f'W/"browser-projection-record:{fingerprint}"',
        "record_version": record_version,
        "record_fingerprint": record_fingerprint,
        "status": status,
        "source": "ProjectionRecord registry" if cached else "browser demand",
        "missing_reason": None if cached else "demanded_projection_record_not_materialized",
    }


def _browser_projection_lifecycle(
    *,
    record_payload: Mapping[str, Any] | None,
    cached: bool,
) -> dict[str, Any]:
    meta = record_payload.get("meta") if isinstance(record_payload, Mapping) else None
    meta_payload = meta if isinstance(meta, Mapping) else {}
    record_status = str(record_payload.get("status") or "").strip().lower() if cached else None
    if not cached:
        state = "pending"
        reason = "demanded_projection_record_not_materialized"
    elif record_status in {"loading", "refreshing", "pending"}:
        state = "refreshing"
        reason = str(meta_payload.get("lifecycle_reason") or record_status or "refreshing")
    elif record_status in {"ready", "stale"}:
        state = record_status
        reason = str(meta_payload.get("lifecycle_reason") or record_status)
    else:
        state = "error"
        reason = str(meta_payload.get("lifecycle_reason") or record_status or "projection_record_error")
    return {
        "state": state,
        "record_status": record_status,
        "reason": reason,
        "ready": state == "ready",
        "terminal": state in {"ready", "stale", "error"},
    }


def _browser_lifecycle_summary(entries: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    state_order = ["pending", "refreshing", "ready", "stale", "error"]
    states = {state: 0 for state in state_order}
    projection_keys_by_state = {state: [] for state in state_order}
    for entry in entries:
        lifecycle = entry.get("lifecycle") if isinstance(entry, Mapping) else None
        lifecycle_payload = lifecycle if isinstance(lifecycle, Mapping) else {}
        state = str(lifecycle_payload.get("state") or "error").strip().lower()
        if state not in states:
            state = "error"
        projection_key = str(entry.get("projection_key") or "").strip()
        states[state] += 1
        if projection_key:
            projection_keys_by_state[state].append(projection_key)
    blocking_states = ["pending", "refreshing", "error"]
    return {
        "states": states,
        "projection_keys_by_state": projection_keys_by_state,
        "ready": all(states[state] == 0 for state in blocking_states),
        "blocked": any(states[state] > 0 for state in blocking_states),
        "pending_projection_keys": projection_keys_by_state["pending"],
        "refreshing_projection_keys": projection_keys_by_state["refreshing"],
        "stale_projection_keys": projection_keys_by_state["stale"],
        "error_projection_keys": projection_keys_by_state["error"],
    }


def browser_projection_record_snapshot(
    *,
    webspace_id: str | None = None,
    client_id: str | None = None,
    session_id: str | None = None,
    projection_keys: Iterable[Any] | None = None,
    include_hidden: bool = True,
    include_stale: bool = True,
    stale_after_s: float | None = None,
    now: float | None = None,
) -> dict[str, Any]:
    """Return a browser-facing view of demanded canonical ProjectionRecords."""

    webspace_token = str(webspace_id or "").strip()
    client_token = str(client_id or "").strip()
    session_token = str(session_id or "").strip()
    requested_keys = _projection_key_filter(projection_keys)
    consumers = projection_demand_consumers(
        webspace_id=webspace_token or None,
        include_hidden=include_hidden,
        include_stale=include_stale,
        stale_after_s=stale_after_s,
        now=now,
    )
    if client_token:
        consumers = [consumer for consumer in consumers if consumer.client_id == client_token]
    if session_token:
        consumers = [consumer for consumer in consumers if consumer.session_id == session_token]
    demanded_keys = sorted(
        {
            consumer.projection_key
            for consumer in consumers
            if consumer.projection_key and (requested_keys is None or consumer.projection_key in requested_keys)
        }
    )
    consumers_by_projection = {
        key: [consumer.to_dict() for consumer in consumers if consumer.projection_key == key]
        for key in demanded_keys
    }
    records_by_projection = {
        record.meta.projection_key: record
        for record in list_projection_records(webspace_id=webspace_token or None)
        if requested_keys is None or record.meta.projection_key in requested_keys
    }
    entries: list[dict[str, Any]] = []
    records: dict[str, Any] = {}
    missing_projection_keys: list[str] = []
    for projection_key in demanded_keys:
        record = records_by_projection.get(projection_key)
        consumer_items = consumers_by_projection.get(projection_key, [])
        if record is None:
            missing_projection_keys.append(projection_key)
            entries.append(
                {
                    "projection_key": projection_key,
                    "cached": False,
                    "record": None,
                    "consumer_total": len(consumer_items),
                    "consumers": consumer_items,
                    "lifecycle": _browser_projection_lifecycle(
                        record_payload=None,
                        cached=False,
                    ),
                    "cache": _browser_cache_entry_metadata(
                        webspace_id=webspace_token or None,
                        client_id=client_token or None,
                        session_id=session_token or None,
                        projection_key=projection_key,
                        record_payload=None,
                        cached=False,
                    ),
                }
            )
            continue
        record_payload = record.to_dict()
        records[projection_key] = record_payload
        entries.append(
            {
                "projection_key": projection_key,
                "cached": True,
                "record": record_payload,
                "consumer_total": len(consumer_items),
                "consumers": consumer_items,
                "lifecycle": _browser_projection_lifecycle(
                    record_payload=record_payload,
                    cached=True,
                ),
                "cache": _browser_cache_entry_metadata(
                    webspace_id=webspace_token or None,
                    client_id=client_token or None,
                    session_id=session_token or None,
                    projection_key=projection_key,
                    record_payload=record_payload,
                    cached=True,
                ),
            }
        )

    cache_key = _browser_cache_key(
        webspace_id=webspace_token or None,
        client_id=client_token or None,
        session_id=session_token or None,
        projection_keys=demanded_keys,
    )
    fingerprint = projection_fingerprint(
        {
            "webspace_id": webspace_token or None,
            "client_id": client_token or None,
            "session_id": session_token or None,
            "projection_keys": demanded_keys,
            "requested_projection_keys": sorted(requested_keys or []),
            "missing_projection_keys": missing_projection_keys,
            "records": records,
            "consumers": consumers_by_projection,
        }
    )
    etag = f'W/"browser-projection-records:{fingerprint}"'
    lifecycle_summary = _browser_lifecycle_summary(entries)

    return {
        "ok": True,
        "accepted": True,
        "webspace_id": webspace_token or None,
        "client_id": client_token or None,
        "session_id": session_token or None,
        "kind": "browser-demanded-projection-records",
        "read_path": "data/projectionRecords.records[projection_key]",
        "demanded_only": True,
        "session_scoped": bool(client_token or session_token),
        "projection_scoped": requested_keys is not None,
        "requested_projection_keys": sorted(requested_keys or []),
        "include_hidden": bool(include_hidden),
        "include_stale": bool(include_stale),
        "demanded_projection_total": len(demanded_keys),
        "record_total": len(records),
        "missing_record_total": len(missing_projection_keys),
        "ready_record_total": sum(1 for item in records.values() if item.get("status") == "ready"),
        "stale_record_total": sum(1 for item in records.values() if item.get("status") == "stale"),
        "error_record_total": sum(1 for item in records.values() if item.get("status") == "error"),
        "lifecycle_summary": lifecycle_summary,
        "projection_keys": demanded_keys,
        "missing_projection_keys": missing_projection_keys,
        "records": records,
        "entries": entries,
        "entry_cache_keys": [str(entry["cache"]["key"]) for entry in entries],
        "entry_fingerprints": {
            str(entry["projection_key"]): str(entry["cache"]["fingerprint"]) for entry in entries
        },
        "entry_etags": {
            str(entry["projection_key"]): str(entry["cache"]["etag"]) for entry in entries
        },
        "fingerprint": fingerprint,
        "etag": etag,
        "cache": {
            "key": cache_key,
            "fingerprint": fingerprint,
            "etag": etag,
            "policy": "no-cache",
            "if_none_match_supported": True,
        },
        "cache_contract": {
            "source": "ProjectionRecord registry",
            "yjs_path": "data/projectionRecords",
            "browser_read": True,
            "browser_write": False,
            "skill_write": False,
            "client_session_filter": True,
            "write_policy": "core-owned-cache-only",
            "legacy_fallback": "compatibility-only",
        },
        "updated_at": float(now if now is not None else time.time()),
    }


def browser_projection_adapter_contract_snapshot(*, now: float | None = None) -> dict[str, Any]:
    """Return the browser adapter contract for ProjectionRecord reads."""

    return {
        "contract": PROJECTION_RECORD_BROWSER_ADAPTER_CONTRACT,
        "ready_for_mvp": True,
        "updated_at": float(now if now is not None else time.time()),
        "source_of_truth": {
            "canonical_yjs_path": "data/projectionRecords",
            "api_read_path": "/api/node/projection-records/browser-cache",
            "demand_source": "/api/node/projection-demand",
            "materialize_path": "/api/node/projection-records/yjs/materialize",
        },
        "adapter_rules": {
            "read_projection_records": True,
            "read_monolithic_scenario_snapshot": "compatibility-only",
            "write_projection_records_from_browser": False,
            "write_projection_records_from_skill": False,
            "cache_by_projection_key": True,
            "reuse_cached_views": True,
            "prefer_nested_projection_path": True,
            "avoid_observe_deep_data": True,
        },
        "cache_model": {
            "browser_cache_key": "browser-projection-records:{webspace_id}:{client_id}:{session_id}:{projection_keys}",
            "entry_cache_key": "browser-projection-records:{webspace_id}:{client_id}:{session_id}:{projection_key}",
            "entry_fingerprints": "entry_fingerprints[projection_key]",
            "entry_etags": "entry_etags[projection_key]",
            "if_none_match": "supported",
            "cache_policy": "no-cache with stable ETag comparison",
        },
        "read_flow": [
            "browser declares active projection demand",
            "core materializes demanded ProjectionRecords",
            "adapter reads browser-cache or data/projectionRecords.records[projection_key]",
            "adapter reuses entry cache when fingerprint/etag is unchanged",
            "adapter observes only projectionRecords or a stable projection-key path where available",
        ],
        "lifecycle_states": ["pending", "refreshing", "ready", "stale", "error"],
        "roadmap_items": [
            "yjs.adapter_projection_records",
            "yjs.cache_by_projection_key",
            "yjs.reuse_cached_views",
            "yjs.reduce_broad_observers",
        ],
        "evidence": [
            "/api/node/projection-records/browser-cache",
            "browser_projection_record_snapshot",
            "entries[].cache.key",
            "entry_fingerprints",
            "entry_etags",
            "lifecycle_summary",
        ],
    }


__all__ = [
    "PROJECTION_RECORD_BROWSER_ADAPTER_CONTRACT",
    "browser_projection_adapter_contract_snapshot",
    "browser_projection_record_snapshot",
    "clear_projection_record_registry",
    "get_projection_record",
    "list_projection_records",
    "projection_record_registry_snapshot",
    "write_projection_record",
    "write_projection_record_if_valid",
]
