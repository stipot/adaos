from __future__ import annotations

import json
import time
from typing import Any, Iterable, Mapping

from adaos.domain import ProjectionRecord, normalize_projection_record, projection_fingerprint
from adaos.services.projection_demand import demanded_projection_keys
from adaos.services.projection_records import list_projection_records, projection_record_registry_snapshot
from adaos.services.yjs.doc import async_get_ydoc, mutate_live_room
from adaos.services.yjs.webspace import default_webspace_id


PROJECTION_RECORDS_YJS_KEY = "projectionRecords"
PROJECTION_RECORDS_YJS_PATH = f"data/{PROJECTION_RECORDS_YJS_KEY}"
PROJECTION_RECORDS_YJS_SCHEMA = "adaos.projection-records.v1"


def _json_clone(value: Any) -> Any:
    return json.loads(json.dumps(value, ensure_ascii=False, sort_keys=True, default=str))


def _webspace_token(value: Any = None) -> str:
    token = str(value or "").strip()
    return token or default_webspace_id()


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
    registry = projection_record_registry_snapshot(webspace_id=target_webspace_id)
    payload = {
        "schema": PROJECTION_RECORDS_YJS_SCHEMA,
        "webspace_id": target_webspace_id,
        "yjs_path": PROJECTION_RECORDS_YJS_PATH,
        "registry_version": registry.get("registry_version"),
        "record_total": len(items),
        "ready_total": sum(1 for record in records if record.status == "ready"),
        "stale_total": sum(1 for record in records if record.status == "stale"),
        "error_total": sum(1 for record in records if record.status == "error"),
        "unavailable_total": sum(1 for record in records if record.status == "unavailable"),
        "demanded_only": bool(demanded_only),
        "projection_keys": sorted(by_key),
        "records": by_key,
        "items": items,
        "updated_at": ts,
    }
    payload["fingerprint"] = projection_fingerprint(
        {
            "schema": payload["schema"],
            "webspace_id": payload["webspace_id"],
            "registry_version": payload["registry_version"],
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
        "record_total": int(payload["record_total"]),
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


__all__ = [
    "PROJECTION_RECORDS_YJS_KEY",
    "PROJECTION_RECORDS_YJS_PATH",
    "PROJECTION_RECORDS_YJS_SCHEMA",
    "build_projection_records_yjs_payload",
    "materialize_projection_records_to_yjs",
    "normalize_projection_record_keys",
]
