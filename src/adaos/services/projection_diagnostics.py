from __future__ import annotations

import time
from typing import Any, Mapping

from adaos.services.projection_demand import projection_demand_snapshot
from adaos.services.projection_dispatcher import projection_dispatcher_snapshot
from adaos.services.projection_records import projection_record_registry_snapshot
from adaos.domain.projection_keys import STATUS_CARD_PROJECTION_PREFIX, status_card_id_from_projection_key
from adaos.services.status_projection import (
    status_card_projection_snapshot,
)


def _handler_for_projection(projection_key: str, handlers: list[str]) -> dict[str, Any]:
    if projection_key in handlers:
        return {"available": True, "key": projection_key, "match": "exact"}
    wildcard_matches = [
        handler
        for handler in handlers
        if handler.endswith("*") and projection_key.startswith(handler[:-1])
    ]
    if wildcard_matches:
        key = max(wildcard_matches, key=lambda item: len(item[:-1]))
        return {"available": True, "key": key, "match": "wildcard"}
    return {"available": False, "key": None, "match": None}


def _projection_key(record: Mapping[str, Any]) -> str:
    meta = record.get("meta") if isinstance(record.get("meta"), Mapping) else {}
    return str(meta.get("projection_key") or "").strip()


def _card_details(card: Mapping[str, Any] | None, record: Mapping[str, Any] | None) -> dict[str, Any] | None:
    if card is None and record is None:
        return None
    details_ref = card.get("details_ref") if isinstance(card, Mapping) else None
    return {
        "id": card.get("id") if isinstance(card, Mapping) else None,
        "published": card is not None,
        "status": card.get("status") if isinstance(card, Mapping) else None,
        "summary": card.get("summary") if isinstance(card, Mapping) else None,
        "version": card.get("version") if isinstance(card, Mapping) else None,
        "details_ref": dict(details_ref) if isinstance(details_ref, Mapping) else details_ref,
        "projection_status": record.get("status") if isinstance(record, Mapping) else None,
        "projection_reason": (
            record.get("meta", {}).get("lifecycle_reason")
            if isinstance(record, Mapping) and isinstance(record.get("meta"), Mapping)
            else None
        ),
    }


def _materialized_record_details(record: Mapping[str, Any] | None) -> dict[str, Any]:
    if record is None:
        return {
            "materialized": False,
            "status": None,
            "version": None,
            "fingerprint": None,
            "lifecycle_reason": None,
            "updated_at": None,
            "changed_at": None,
            "error": None,
        }
    meta = record.get("meta") if isinstance(record.get("meta"), Mapping) else {}
    return {
        "materialized": True,
        "status": record.get("status"),
        "version": meta.get("version"),
        "fingerprint": meta.get("fingerprint"),
        "lifecycle_reason": meta.get("lifecycle_reason"),
        "updated_at": meta.get("updated_at"),
        "changed_at": meta.get("changed_at"),
        "error": record.get("error"),
    }


def _yjs_cache_record_details(
    projection_key: str,
    *,
    yjs_cache: Mapping[str, Any] | None,
) -> dict[str, Any]:
    cache_present = bool(isinstance(yjs_cache, Mapping) and yjs_cache.get("cache_present"))
    if not cache_present:
        return {
            "cached": False,
            "cache_present": cache_present,
            "status": None,
            "version": None,
            "fingerprint": None,
            "schema_ok": None,
            "fingerprint_ok": None,
            "updated_at": None,
        }
    payload = yjs_cache.get("payload") if isinstance(yjs_cache.get("payload"), Mapping) else {}
    records = payload.get("records") if isinstance(payload.get("records"), Mapping) else {}
    record = records.get(projection_key) if isinstance(records.get(projection_key), Mapping) else None
    if record is None:
        return {
            "cached": False,
            "cache_present": True,
            "status": None,
            "version": None,
            "fingerprint": None,
            "schema_ok": yjs_cache.get("schema_ok"),
            "fingerprint_ok": yjs_cache.get("fingerprint_ok"),
            "updated_at": payload.get("updated_at"),
        }
    meta = record.get("meta") if isinstance(record.get("meta"), Mapping) else {}
    return {
        "cached": True,
        "cache_present": True,
        "status": record.get("status"),
        "version": meta.get("version"),
        "fingerprint": meta.get("fingerprint"),
        "schema_ok": yjs_cache.get("schema_ok"),
        "fingerprint_ok": yjs_cache.get("fingerprint_ok"),
        "updated_at": payload.get("updated_at"),
    }


def projection_operator_diagnostics(
    *,
    webspace_id: str | None = None,
    include_stale: bool = True,
    stale_after_s: float | None = None,
    now: float | None = None,
    yjs_cache: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    ts = float(now if now is not None else time.time())
    demand = projection_demand_snapshot(
        webspace_id=webspace_id,
        include_stale=include_stale,
        stale_after_s=stale_after_s,
        now=ts,
    )
    dispatcher = projection_dispatcher_snapshot()
    registry = status_card_projection_snapshot(webspace_id=webspace_id, now=ts)
    projection_registry = projection_record_registry_snapshot(webspace_id=webspace_id)
    handlers = [str(item) for item in dispatcher.get("handlers", [])]
    records_by_projection = {
        _projection_key(record): record
        for record in registry.get("records", [])
        if isinstance(record, Mapping) and _projection_key(record)
    }
    materialized_records_by_projection = {
        _projection_key(record): record
        for record in projection_registry.get("records", [])
        if isinstance(record, Mapping) and _projection_key(record)
    }
    cards_by_id = {
        str(card.get("id") or ""): card
        for card in registry.get("cards", [])
        if isinstance(card, Mapping) and str(card.get("id") or "").strip()
    }

    active: list[dict[str, Any]] = []
    for projection in demand.get("projections", []):
        if not isinstance(projection, Mapping):
            continue
        projection_key = str(projection.get("projection_key") or "").strip()
        handler = _handler_for_projection(projection_key, handlers)
        card_id = None
        card = None
        record = records_by_projection.get(projection_key)
        materialized_record = materialized_records_by_projection.get(projection_key)
        if projection_key.startswith(STATUS_CARD_PROJECTION_PREFIX):
            try:
                card_id = status_card_id_from_projection_key(projection_key)
            except ValueError:
                card_id = None
            if card_id:
                card = cards_by_id.get(card_id)
        yjs_cache_record = _yjs_cache_record_details(projection_key, yjs_cache=yjs_cache)
        active.append(
            {
                "projection_key": projection_key,
                "consumer_total": int(projection.get("consumer_total") or 0),
                "visible_total": int(projection.get("visible_total") or 0),
                "pinned_total": int(projection.get("pinned_total") or 0),
                "stale_total": int(projection.get("stale_total") or 0),
                "handler": handler,
                "status_card": _card_details(card, record),
                "projection_record": _materialized_record_details(materialized_record),
                "yjs_cache_record": yjs_cache_record if yjs_cache is not None else None,
                "consumers": list(projection.get("consumers") or []),
            }
        )

    missing_handler_total = sum(1 for item in active if not item["handler"]["available"])
    missing_status_card_total = sum(
        1
        for item in active
        if item["projection_key"].startswith(STATUS_CARD_PROJECTION_PREFIX)
        and not (item.get("status_card") or {}).get("published")
    )
    stale_projection_total = sum(1 for item in active if int(item.get("stale_total") or 0) > 0)
    materialized_projection_total = sum(
        1 for item in active if (item.get("projection_record") or {}).get("materialized")
    )
    yjs_cache_checked = yjs_cache is not None
    yjs_cache_projection_total = (
        sum(1 for item in active if (item.get("yjs_cache_record") or {}).get("cached"))
        if yjs_cache_checked
        else 0
    )
    yjs_cache_envelope = None
    if isinstance(yjs_cache, Mapping) and isinstance(yjs_cache.get("envelope"), Mapping):
        yjs_cache_envelope = dict(yjs_cache.get("envelope") or {})
    return {
        "ok": True,
        "webspace_id": str(webspace_id or "").strip() or None,
        "active_projection_total": len(active),
        "active_consumer_total": sum(int(item.get("consumer_total") or 0) for item in active),
        "missing_handler_total": missing_handler_total,
        "missing_status_card_total": missing_status_card_total,
        "materialized_projection_total": materialized_projection_total,
        "missing_projection_record_total": len(active) - materialized_projection_total,
        "yjs_cache_checked": yjs_cache_checked,
        "yjs_cache_projection_total": yjs_cache_projection_total,
        "yjs_cache_envelope_ok": yjs_cache.get("envelope_ok") if isinstance(yjs_cache, Mapping) else None,
        "yjs_cache_envelope": yjs_cache_envelope,
        "yjs_cache_node_ids": list(yjs_cache.get("node_ids") or []) if isinstance(yjs_cache, Mapping) else [],
        "missing_yjs_cache_projection_total": (
            len(active) - yjs_cache_projection_total if yjs_cache_checked else 0
        ),
        "stale_projection_total": stale_projection_total,
        "active_projections": active,
        "demand": demand,
        "dispatcher": dispatcher,
        "status_registry": registry,
        "projection_registry": projection_registry,
        "yjs_cache": dict(yjs_cache) if isinstance(yjs_cache, Mapping) else None,
        "updated_at": ts,
    }


__all__ = ["projection_operator_diagnostics"]
