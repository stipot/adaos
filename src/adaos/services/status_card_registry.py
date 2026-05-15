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
)
from adaos.services.projection_dispatcher import (
    ProjectionRefreshContext,
    ProjectionRefreshResult,
    register_projection_refresh_handler,
    unregister_projection_refresh_handler,
)


STATUS_CARD_PROJECTION_PREFIX = "status-card:"
STATUS_CARD_WILDCARD_HANDLER = f"{STATUS_CARD_PROJECTION_PREFIX}*"

_LOCK = RLock()
_CARDS: dict[tuple[str, str], StatusCard] = {}
_STATS: dict[str, int] = {
    "publish_total": 0,
    "changed_total": 0,
    "unchanged_total": 0,
}


def status_card_projection_key(card_id: str) -> str:
    token = str(card_id or "").strip()
    if not token:
        raise ValueError("card_id is required")
    return f"{STATUS_CARD_PROJECTION_PREFIX}{token}"


def status_card_id_from_projection_key(projection_key: str) -> str:
    token = str(projection_key or "").strip()
    if not token.startswith(STATUS_CARD_PROJECTION_PREFIX):
        raise ValueError("status-card projection key is required")
    card_id = token[len(STATUS_CARD_PROJECTION_PREFIX) :].strip()
    if not card_id:
        raise ValueError("status-card id is required")
    return card_id


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
        for key in list(_STATS):
            _STATS[key] = 0


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
        _STATS["publish_total"] = int(_STATS.get("publish_total") or 0) + 1
        if previous is not None and previous.fingerprint == card.fingerprint:
            _STATS["unchanged_total"] = int(_STATS.get("unchanged_total") or 0) + 1
        else:
            _STATS["changed_total"] = int(_STATS.get("changed_total") or 0) + 1
        return card


def write_status_card(card: StatusCard) -> StatusCard:
    key = _registry_key(webspace_id=str(card.webspace_id or ""), card_id=card.id)
    with _LOCK:
        previous = _CARDS.get(key)
        _CARDS[key] = card
        _STATS["publish_total"] = int(_STATS.get("publish_total") or 0) + 1
        if previous is not None and previous.fingerprint == card.fingerprint:
            _STATS["unchanged_total"] = int(_STATS.get("unchanged_total") or 0) + 1
        else:
            _STATS["changed_total"] = int(_STATS.get("changed_total") or 0) + 1
    return card


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


__all__ = [
    "STATUS_CARD_PROJECTION_PREFIX",
    "STATUS_CARD_WILDCARD_HANDLER",
    "clear_status_card_registry",
    "ensure_status_card_dispatcher_handler",
    "get_status_card",
    "list_status_cards",
    "publish_status_card",
    "refresh_status_card_projection",
    "remove_status_card_dispatcher_handler",
    "status_card_id_from_projection_key",
    "status_card_projection_key",
    "status_card_projection_record",
    "status_card_registry_snapshot",
    "write_status_card",
]
