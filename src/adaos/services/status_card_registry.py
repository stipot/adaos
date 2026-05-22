from __future__ import annotations

import time
from threading import RLock
from typing import Any, Mapping

from adaos.domain import (
    ProjectionRecord,
    ProjectionStatus,
    StatusCard,
    is_status_card_stale,
    make_status_card,
    make_status_card_projection_record,
    status_card_id_from_projection_key as _domain_status_card_id_from_projection_key,
    status_card_projection_key as _domain_status_card_projection_key,
)
from adaos.services.projection_dispatcher import (
    ProjectionRefreshContext,
    ProjectionRefreshResult,
    register_projection_refresh_handler,
    unregister_projection_refresh_handler,
)
from adaos.services.projection_demand import demanded_projection_keys
from adaos.services.projection_records import projection_record_registry_snapshot, write_projection_record


STATUS_CARD_PROJECTION_PREFIX = "status-card:"
STATUS_CARD_WILDCARD_HANDLER = f"{STATUS_CARD_PROJECTION_PREFIX}*"

_LOCK = RLock()
_CARDS: dict[tuple[str, str], StatusCard] = {}
_DEFAULT_STATS: dict[str, float | int | None] = {
    "registry_version": 0,
    "publish_total": 0,
    "changed_total": 0,
    "unchanged_total": 0,
    "last_publish_at": None,
    "last_publish_latency_ms": None,
    "sweep_total": 0,
    "swept_total": 0,
}
_STATS: dict[str, float | int | None] = dict(_DEFAULT_STATS)


def status_card_projection_key(card_id: str) -> str:
    return _domain_status_card_projection_key(card_id)


def status_card_id_from_projection_key(projection_key: str) -> str:
    return _domain_status_card_id_from_projection_key(projection_key)


def _status_card_id_token(value: Any) -> str:
    token = str(value or "").strip()
    if token.startswith(STATUS_CARD_PROJECTION_PREFIX):
        token = token[len(STATUS_CARD_PROJECTION_PREFIX) :].strip()
    return token


def _registry_key(*, webspace_id: str, card_id: str) -> tuple[str, str]:
    webspace_token = str(webspace_id or "").strip()
    card_token = str(card_id or "").strip()
    if not webspace_token:
        raise ValueError("webspace_id is required")
    if not card_token:
        raise ValueError("card_id is required")
    return (webspace_token, card_token)


def clear_status_card_registry() -> None:
    with _LOCK:
        _CARDS.clear()
        _STATS.clear()
        _STATS.update(_DEFAULT_STATS)


def publish_status_card(
    *,
    id: str,
    owner: str,
    kind: str,
    scope: Any,
    status: Any,
    summary: str,
    webspace_id: str,
    severity: str | None = None,
    updated_at: float | None = None,
    ttl_ms: int | None = None,
    details_ref: Mapping[str, Any] | None = None,
    incident_id: str | None = None,
) -> StatusCard:
    started_at = time.perf_counter()
    key = _registry_key(webspace_id=webspace_id, card_id=id)
    with _LOCK:
        previous = _CARDS.get(key)
        card = make_status_card(
            id=id,
            owner=owner,
            kind=kind,
            scope=scope,
            webspace_id=key[0],
            status=status,
            summary=summary,
            severity=severity,
            updated_at=updated_at,
            ttl_ms=ttl_ms,
            details_ref=details_ref,
            incident_id=incident_id,
            previous=previous,
        )
        _CARDS[key] = card
        _record_publish_stats(previous=previous, card=card, started_at=started_at)
        return card


def write_status_card(card: StatusCard) -> StatusCard:
    started_at = time.perf_counter()
    key = _registry_key(webspace_id=str(card.webspace_id or ""), card_id=card.id)
    with _LOCK:
        previous = _CARDS.get(key)
        _CARDS[key] = card
        _record_publish_stats(previous=previous, card=card, started_at=started_at)
    return card


def _record_publish_stats(*, previous: StatusCard | None, card: StatusCard, started_at: float) -> None:
    _STATS["publish_total"] = int(_STATS.get("publish_total") or 0) + 1
    if previous is not None and previous.fingerprint == card.fingerprint:
        _STATS["unchanged_total"] = int(_STATS.get("unchanged_total") or 0) + 1
    else:
        _STATS["changed_total"] = int(_STATS.get("changed_total") or 0) + 1
        _STATS["registry_version"] = int(_STATS.get("registry_version") or 0) + 1
    _STATS["last_publish_at"] = float(time.time())
    _STATS["last_publish_latency_ms"] = round(max(0.0, time.perf_counter() - started_at) * 1000.0, 3)


def get_status_card(*, card_id: str, webspace_id: str) -> StatusCard | None:
    key = _registry_key(webspace_id=webspace_id, card_id=card_id)
    with _LOCK:
        return _CARDS.get(key)


def list_status_cards(*, webspace_id: str | None = None) -> list[StatusCard]:
    webspace_token = str(webspace_id or "").strip()
    with _LOCK:
        cards = list(_CARDS.values())
    if webspace_token:
        cards = [card for card in cards if card.webspace_id == webspace_token]
    return sorted(cards, key=lambda item: (str(item.webspace_id or ""), item.id))


def status_card_projection_record(
    *,
    card_id: str,
    webspace_id: str,
    node_id: str | None = None,
    source_authority: str = "platform-status-registry",
    access: Mapping[str, Any] | None = None,
    now: float | None = None,
) -> ProjectionRecord | None:
    card = get_status_card(card_id=card_id, webspace_id=webspace_id)
    if card is None:
        return None
    stale = is_status_card_stale(card, now=now)
    return make_status_card_projection_record(
        card,
        webspace_id=webspace_id,
        node_id=node_id,
        source_authority=source_authority,
        access=access,
        status=ProjectionStatus.STALE if stale else ProjectionStatus.READY,
        lifecycle_reason="ttl_expired" if stale else "materialized",
    )


def refresh_status_card_projection(context: ProjectionRefreshContext) -> ProjectionRefreshResult:
    card_id = status_card_id_from_projection_key(context.projection_key)
    record = status_card_projection_record(
        card_id=card_id,
        webspace_id=context.webspace_id,
        access={"visibility": "operator"},
        now=context.requested_at,
    )
    if record is None:
        return ProjectionRefreshResult(
            projection_key=context.projection_key,
            webspace_id=context.webspace_id,
            status=ProjectionStatus.UNAVAILABLE.value,
            reason="status_card_missing",
        )
    return ProjectionRefreshResult(
        projection_key=context.projection_key,
        webspace_id=context.webspace_id,
        status=record.status,
        record=record.to_dict(),
        reason=record.meta.lifecycle_reason,
    )


def ensure_status_card_dispatcher_handler() -> None:
    register_projection_refresh_handler(STATUS_CARD_WILDCARD_HANDLER, refresh_status_card_projection)


def remove_status_card_dispatcher_handler() -> bool:
    return unregister_projection_refresh_handler(STATUS_CARD_WILDCARD_HANDLER)


def sweep_status_card_registry(
    *,
    webspace_id: str | None = None,
    now: float | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    ts = float(now if now is not None else time.time())
    webspace_token = str(webspace_id or "").strip()
    with _LOCK:
        stale_items = [
            (key, card)
            for key, card in sorted(_CARDS.items(), key=lambda item: (item[0][0], item[0][1]))
            if (not webspace_token or key[0] == webspace_token) and is_status_card_stale(card, now=ts)
        ]
        if not dry_run:
            for key, _ in stale_items:
                _CARDS.pop(key, None)
            _STATS["sweep_total"] = int(_STATS.get("sweep_total") or 0) + 1
            _STATS["swept_total"] = int(_STATS.get("swept_total") or 0) + len(stale_items)
            if stale_items:
                _STATS["registry_version"] = int(_STATS.get("registry_version") or 0) + 1
        stats = dict(_STATS)
    return {
        "ok": True,
        "accepted": not dry_run,
        "dry_run": bool(dry_run),
        "webspace_id": webspace_token or None,
        "stale_total": len(stale_items),
        "removed_total": 0 if dry_run else len(stale_items),
        "cards": [card.to_dict() for _, card in stale_items],
        "stats": stats,
        "updated_at": ts,
    }


def status_card_registry_snapshot(*, webspace_id: str | None = None, now: float | None = None) -> dict[str, Any]:
    ts = float(now if now is not None else time.time())
    cards = list_status_cards(webspace_id=webspace_id)
    records = [
        record.to_dict()
        for card in cards
        for record in [
            status_card_projection_record(
                card_id=card.id,
                webspace_id=str(card.webspace_id or ""),
                access={"visibility": "operator"},
                now=ts,
            )
        ]
        if record is not None
    ]
    stale_total = sum(1 for record in records if record.get("status") == ProjectionStatus.STALE.value)
    with _LOCK:
        stats = dict(_STATS)
    return {
        "ok": True,
        "webspace_id": str(webspace_id or "").strip() or None,
        "registry_version": int(stats.get("registry_version") or 0),
        "card_total": len(cards),
        "projection_total": len(records),
        "stale_total": stale_total,
        "ready_total": sum(1 for record in records if record.get("status") == ProjectionStatus.READY.value),
        "stats": {
            **stats,
            "stale_total": stale_total,
            "ready_total": sum(1 for record in records if record.get("status") == ProjectionStatus.READY.value),
        },
        "cards": [card.to_dict() for card in cards],
        "records": records,
        "updated_at": ts,
    }


def materialize_status_card_projection_records(
    *,
    webspace_id: str | None = None,
    card_ids: list[str] | tuple[str, ...] | set[str] | None = None,
    demanded_only: bool = False,
    now: float | None = None,
    access: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    webspace_token = str(webspace_id or "").strip()
    requested_ids: set[str] | None = None
    if card_ids:
        requested_ids = {
            card_id
            for card_id in (_status_card_id_token(item) for item in card_ids)
            if card_id
        }
    if demanded_only:
        demanded_ids = {
            card_id
            for card_id in (
                _status_card_id_token(token)
                for token in demanded_projection_keys(webspace_id=webspace_token or None)
                if str(token or "").strip().startswith(STATUS_CARD_PROJECTION_PREFIX)
            )
            if card_id
        }
        requested_ids = demanded_ids if requested_ids is None else requested_ids.intersection(demanded_ids)
    records: list[ProjectionRecord] = []
    for card in list_status_cards(webspace_id=webspace_token or None):
        if requested_ids is not None and card.id not in requested_ids:
            continue
        record = status_card_projection_record(
            card_id=card.id,
            webspace_id=str(card.webspace_id or ""),
            access=access or {"visibility": "operator"},
            now=now,
        )
        if record is not None:
            records.append(write_projection_record(record))
    return {
        "ok": True,
        "accepted": True,
        "webspace_id": webspace_token or None,
        "demanded_only": bool(demanded_only),
        "requested_card_ids": sorted(requested_ids) if requested_ids is not None else None,
        "materialized_total": len(records),
        "records": [record.to_dict() for record in records],
        "projection_registry": projection_record_registry_snapshot(webspace_id=webspace_token or None),
        "updated_at": float(now if now is not None else time.time()),
    }


__all__ = [
    "STATUS_CARD_PROJECTION_PREFIX",
    "STATUS_CARD_WILDCARD_HANDLER",
    "clear_status_card_registry",
    "ensure_status_card_dispatcher_handler",
    "get_status_card",
    "list_status_cards",
    "materialize_status_card_projection_records",
    "publish_status_card",
    "refresh_status_card_projection",
    "remove_status_card_dispatcher_handler",
    "status_card_id_from_projection_key",
    "status_card_projection_key",
    "status_card_projection_record",
    "status_card_registry_snapshot",
    "sweep_status_card_registry",
    "write_status_card",
]
