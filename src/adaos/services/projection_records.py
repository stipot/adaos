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


def browser_projection_record_snapshot(
    *,
    webspace_id: str | None = None,
    projection_keys: Iterable[Any] | None = None,
    include_hidden: bool = True,
    include_stale: bool = True,
    stale_after_s: float | None = None,
    now: float | None = None,
) -> dict[str, Any]:
    """Return a browser-facing view of demanded canonical ProjectionRecords."""

    webspace_token = str(webspace_id or "").strip()
    requested_keys = _projection_key_filter(projection_keys)
    consumers = projection_demand_consumers(
        webspace_id=webspace_token or None,
        include_hidden=include_hidden,
        include_stale=include_stale,
        stale_after_s=stale_after_s,
        now=now,
    )
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
            }
        )

    return {
        "ok": True,
        "accepted": True,
        "webspace_id": webspace_token or None,
        "kind": "browser-demanded-projection-records",
        "read_path": "data/projectionRecords.records[projection_key]",
        "demanded_only": True,
        "include_hidden": bool(include_hidden),
        "include_stale": bool(include_stale),
        "demanded_projection_total": len(demanded_keys),
        "record_total": len(records),
        "missing_record_total": len(missing_projection_keys),
        "ready_record_total": sum(1 for item in records.values() if item.get("status") == "ready"),
        "stale_record_total": sum(1 for item in records.values() if item.get("status") == "stale"),
        "error_record_total": sum(1 for item in records.values() if item.get("status") == "error"),
        "projection_keys": demanded_keys,
        "missing_projection_keys": missing_projection_keys,
        "records": records,
        "entries": entries,
        "cache_contract": {
            "source": "ProjectionRecord registry",
            "yjs_path": "data/projectionRecords",
            "browser_read": True,
            "browser_write": False,
            "skill_write": False,
            "write_policy": "core-owned-cache-only",
            "legacy_fallback": "compatibility-only",
        },
        "updated_at": float(now if now is not None else time.time()),
    }


__all__ = [
    "browser_projection_record_snapshot",
    "clear_projection_record_registry",
    "get_projection_record",
    "list_projection_records",
    "projection_record_registry_snapshot",
    "write_projection_record",
    "write_projection_record_if_valid",
]
