from __future__ import annotations

import time
from threading import RLock
from typing import Any, Mapping

from adaos.domain import EventEnvelope, normalize_event_envelope, status_card_projection_key
from adaos.domain.projection_keys import STATUS_CARD_PROJECTION_PREFIX
from adaos.services.projection_demand import projection_demand_consumers
from adaos.services.projection_dispatcher import dispatch_demanded_projection_refresh
from adaos.services.projection_record_yjs import materialize_projection_records_to_yjs
from adaos.services.status_projection import ensure_status_card_projection_handler


PROJECTION_EVENT_BRIDGE_CONTRACT = "adaos.projection-event-bridge.v1"
STATUS_CARD_CHANGED_EVENT = "adaos.status.card.changed"
PROJECTION_LIFECYCLE_EVENT = "adaos.projection.lifecycle.changed"

_LOCK = RLock()
_REGISTERED_BUSES: set[int] = set()
_STATS: dict[str, int] = {
    "registered_bus_total": 0,
    "incoming_total": 0,
    "selected_total": 0,
    "refreshed_total": 0,
    "skipped_total": 0,
    "error_total": 0,
    "materialized_total": 0,
}


def _inc(name: str, amount: int = 1) -> None:
    with _LOCK:
        _STATS[name] = int(_STATS.get(name) or 0) + int(amount)


def _payload_mapping(envelope: EventEnvelope) -> Mapping[str, Any]:
    return envelope.payload if isinstance(envelope.payload, Mapping) else {}


def _status_card_projection_key_from_event(envelope: EventEnvelope) -> str | None:
    payload = _payload_mapping(envelope)
    card = payload.get("card") if isinstance(payload.get("card"), Mapping) else {}
    card_id = str(card.get("id") or payload.get("card_id") or "").strip()
    if not card_id:
        projection_key = str(payload.get("projection_key") or "").strip()
        if projection_key.startswith(STATUS_CARD_PROJECTION_PREFIX):
            return projection_key
        return None
    return status_card_projection_key(card_id)


def _webspace_ids_from_event(envelope: EventEnvelope, *, projection_key: str) -> list[str]:
    payload = _payload_mapping(envelope)
    card = payload.get("card") if isinstance(payload.get("card"), Mapping) else {}
    scope = envelope.scope if isinstance(envelope.scope, Mapping) else {}
    candidates = [
        scope.get("webspace_id"),
        payload.get("webspace_id"),
        payload.get("workspace_id"),
        card.get("webspace_id"),
    ]
    out: list[str] = []
    for item in candidates:
        token = str(item or "").strip()
        if token and token not in out:
            out.append(token)
    if out:
        return out
    for consumer in projection_demand_consumers(projection_key=projection_key):
        token = str(consumer.webspace_id or "").strip()
        if token and token not in out:
            out.append(token)
    return out


async def handle_status_card_changed_event(event: Any, *, bus: Any | None = None) -> dict[str, Any]:
    envelope = normalize_event_envelope(event)
    projection_key = _status_card_projection_key_from_event(envelope)
    _inc("incoming_total")
    if not projection_key:
        _inc("skipped_total")
        return {
            "ok": True,
            "accepted": False,
            "reason": "status_card_projection_key_missing",
            "event_type": envelope.type,
        }
    webspace_ids = _webspace_ids_from_event(envelope, projection_key=projection_key)
    if not webspace_ids:
        _inc("skipped_total")
        return {
            "ok": True,
            "accepted": False,
            "reason": "no_projection_demand",
            "event_type": envelope.type,
            "projection_key": projection_key,
        }
    ensure_status_card_projection_handler()
    report = await dispatch_demanded_projection_refresh(
        envelope,
        webspace_ids=webspace_ids,
        projection_keys=[projection_key],
        bus=bus,
    )
    _inc("selected_total", len(report.selected))
    _inc("refreshed_total", len(report.refreshed))
    _inc("skipped_total", len(report.skipped))
    _inc("error_total", len(report.errors))
    materialized: list[dict[str, Any]] = []
    refreshed_webspaces = sorted({item.webspace_id for item in report.refreshed})
    for webspace_id in refreshed_webspaces:
        try:
            result = await materialize_projection_records_to_yjs(
                webspace_id=webspace_id,
                projection_keys=[projection_key],
                demanded_only=True,
            )
        except Exception as exc:
            result = {
                "ok": False,
                "accepted": False,
                "webspace_id": webspace_id,
                "projection_keys": [projection_key],
                "error": f"{type(exc).__name__}: {exc}",
            }
        if result.get("ok"):
            _inc("materialized_total")
        else:
            _inc("error_total")
        materialized.append(result)
    return {
        "ok": True,
        "accepted": bool(report.selected),
        "event_type": envelope.type,
        "projection_key": projection_key,
        "webspace_ids": webspace_ids,
        "report": report.to_dict(),
        "materialized": materialized,
        "updated_at": time.time(),
    }


def register_projection_event_bridge(bus: Any) -> dict[str, Any]:
    if bus is None:
        return {"ok": False, "accepted": False, "reason": "bus_missing"}
    key = id(bus)
    with _LOCK:
        if key in _REGISTERED_BUSES:
            return {
                "ok": True,
                "accepted": False,
                "reason": "already_registered",
                "topics": [STATUS_CARD_CHANGED_EVENT],
            }
        _REGISTERED_BUSES.add(key)
        _STATS["registered_bus_total"] = len(_REGISTERED_BUSES)

    async def _status_card_changed(event: Any) -> None:
        await handle_status_card_changed_event(event, bus=bus)

    setattr(_status_card_changed, "_adaos_projection_event_bridge", True)
    bus.subscribe(STATUS_CARD_CHANGED_EVENT, _status_card_changed)
    return {
        "ok": True,
        "accepted": True,
        "topics": [STATUS_CARD_CHANGED_EVENT],
    }


def projection_event_bridge_snapshot(*, now: float | None = None) -> dict[str, Any]:
    with _LOCK:
        stats = dict(_STATS)
        registered_bus_total = len(_REGISTERED_BUSES)
    return {
        "ok": True,
        "contract": PROJECTION_EVENT_BRIDGE_CONTRACT,
        "ready_for_mvp": True,
        "registered_bus_total": registered_bus_total,
        "topics": [STATUS_CARD_CHANGED_EVENT],
        "projection_families": [f"{STATUS_CARD_PROJECTION_PREFIX}*"],
        "pipeline": [
            "eventbus topic",
            "projection demand selection",
            "dispatcher refresh",
            "ProjectionRecord registry",
            "set-if-changed data/projectionRecords materialization",
        ],
        "lifecycle_event": PROJECTION_LIFECYCLE_EVENT,
        "stats": stats,
        "updated_at": float(now if now is not None else time.time()),
    }


__all__ = [
    "PROJECTION_EVENT_BRIDGE_CONTRACT",
    "PROJECTION_LIFECYCLE_EVENT",
    "STATUS_CARD_CHANGED_EVENT",
    "handle_status_card_changed_event",
    "projection_event_bridge_snapshot",
    "register_projection_event_bridge",
]
