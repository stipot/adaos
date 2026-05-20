from __future__ import annotations

import time
from threading import RLock
from typing import Any, Mapping

from adaos.domain import ProjectionRecord, normalize_projection_record, projection_fingerprint


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


__all__ = [
    "clear_projection_record_registry",
    "get_projection_record",
    "list_projection_records",
    "projection_record_registry_snapshot",
    "write_projection_record",
    "write_projection_record_if_valid",
]
