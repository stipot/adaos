from __future__ import annotations

import time
from typing import Any, Iterable, Mapping

from adaos.domain import ProjectionRecord, ProjectionStatus, make_projection_record
from adaos.domain.projection_keys import (
    STATUS_CARD_PROJECTION_PREFIX,
    status_card_id_from_projection_key,
    status_card_projection_key,
)
from adaos.services.agent_context import get_ctx
from adaos.services.projection_demand import demanded_projection_keys
from adaos.services.projection_dispatcher import (
    ProjectionRefreshContext,
    ProjectionRefreshResult,
    register_projection_refresh_handler,
    unregister_projection_refresh_handler,
)
from adaos.services.projection_records import projection_record_registry_snapshot, write_projection_record


STATUS_CARD_WILDCARD_HANDLER = f"{STATUS_CARD_PROJECTION_PREFIX}*"
STATUS_CARD_PROJECTION_KIND = "status-card"


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _status_registry_snapshot(
    *,
    webspace_id: str | None = None,
    include_stale: bool = True,
    now: float | None = None,
) -> dict[str, Any]:
    try:
        registry = get_ctx().status_registry
        return registry.snapshot(
            webspace_id=webspace_id,
            include_stale=include_stale,
            now_ts=float(now if now is not None else time.time()),
        )
    except Exception as exc:
        return {
            "schema": "adaos.status_registry.v1",
            "available": False,
            "updated_at": float(now if now is not None else time.time()),
            "cards": [],
            "total": 0,
            "diagnostics": {"error": f"{type(exc).__name__}: {exc}"},
        }


def _node_id_from_card(card: Mapping[str, Any]) -> str | None:
    for container_name in ("metadata", "route", "guard_ref"):
        container = card.get(container_name)
        if isinstance(container, Mapping):
            token = str(container.get("node_id") or container.get("target_node_id") or "").strip()
            if token:
                return token
    return None


def status_card_projection_record(
    card: Mapping[str, Any],
    *,
    webspace_id: str | None = None,
    access: Mapping[str, Any] | None = None,
) -> ProjectionRecord:
    card_payload = dict(card)
    card_id = str(card_payload.get("id") or "").strip()
    if not card_id:
        raise ValueError("status card id is required")
    card_webspace_id = str(webspace_id or card_payload.get("webspace_id") or "").strip()
    if not card_webspace_id:
        raise ValueError("webspace_id is required")
    stale = bool(card_payload.get("stale"))
    return make_projection_record(
        projection_key=status_card_projection_key(card_id),
        kind=STATUS_CARD_PROJECTION_KIND,
        data=card_payload,
        webspace_id=card_webspace_id,
        status=ProjectionStatus.STALE if stale else ProjectionStatus.READY,
        node_id=_node_id_from_card(card_payload),
        version=card_payload.get("version"),
        fingerprint=card_payload.get("fingerprint"),
        source=str(card_payload.get("owner") or "status_registry"),
        source_authority="status_registry",
        access=access or {"audience": "shared", "visibility": "operator"},
        lifecycle_reason="ttl_expired" if stale else "materialized",
        updated_at=card_payload.get("updated_at"),
        changed_at=card_payload.get("changed_at"),
    )


def status_card_projection_snapshot(
    *,
    webspace_id: str | None = None,
    include_stale: bool = True,
    now: float | None = None,
) -> dict[str, Any]:
    snapshot = _status_registry_snapshot(webspace_id=webspace_id, include_stale=include_stale, now=now)
    cards = [card for card in snapshot.get("cards", []) if isinstance(card, Mapping)]
    records = []
    for card in cards:
        try:
            records.append(status_card_projection_record(card, webspace_id=webspace_id).to_dict())
        except ValueError:
            continue
    return {
        "ok": True,
        "available": bool(snapshot.get("available", True)),
        "schema": "adaos.status-card-projections.v1",
        "webspace_id": str(webspace_id or "").strip() or None,
        "card_total": len(cards),
        "projection_total": len(records),
        "ready_total": sum(1 for record in records if record.get("status") == ProjectionStatus.READY.value),
        "stale_total": sum(1 for record in records if record.get("status") == ProjectionStatus.STALE.value),
        "cards": cards,
        "records": records,
        "status_registry": snapshot,
        "updated_at": float(now if now is not None else time.time()),
    }


def refresh_status_card_projection(context: ProjectionRefreshContext) -> ProjectionRefreshResult:
    try:
        card_id = status_card_id_from_projection_key(context.projection_key)
    except ValueError:
        return ProjectionRefreshResult(
            projection_key=context.projection_key,
            webspace_id=context.webspace_id,
            status=ProjectionStatus.UNAVAILABLE.value,
            reason="status_card_projection_key_invalid",
        )
    snapshot = _status_registry_snapshot(webspace_id=context.webspace_id, now=context.requested_at)
    for card in snapshot.get("cards", []):
        if isinstance(card, Mapping) and str(card.get("id") or "").strip() == card_id:
            record = status_card_projection_record(
                card,
                webspace_id=context.webspace_id,
                access={"audience": "shared", "visibility": "operator"},
            )
            return ProjectionRefreshResult(
                projection_key=context.projection_key,
                webspace_id=context.webspace_id,
                status=record.status,
                record=record.to_dict(),
                reason=record.meta.lifecycle_reason,
            )
    return ProjectionRefreshResult(
        projection_key=context.projection_key,
        webspace_id=context.webspace_id,
        status=ProjectionStatus.UNAVAILABLE.value,
        reason="status_card_missing",
    )


def ensure_status_card_projection_handler() -> None:
    register_projection_refresh_handler(STATUS_CARD_WILDCARD_HANDLER, refresh_status_card_projection)


def remove_status_card_projection_handler() -> bool:
    return unregister_projection_refresh_handler(STATUS_CARD_WILDCARD_HANDLER)


def _status_card_id_token(value: Any) -> str:
    token = str(value or "").strip()
    if token.startswith(STATUS_CARD_PROJECTION_PREFIX):
        token = token[len(STATUS_CARD_PROJECTION_PREFIX) :].strip()
    return token


def materialize_status_card_projection_records(
    *,
    webspace_id: str | None = None,
    card_ids: Iterable[Any] | None = None,
    demanded_only: bool = False,
    now: float | None = None,
    access: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    requested_ids = {_status_card_id_token(item) for item in card_ids or [] if _status_card_id_token(item)} or None
    if demanded_only:
        demanded_ids = {
            _status_card_id_token(key)
            for key in demanded_projection_keys(webspace_id=webspace_id)
            if str(key or "").strip().startswith(STATUS_CARD_PROJECTION_PREFIX)
        }
        requested_ids = demanded_ids if requested_ids is None else requested_ids.intersection(demanded_ids)
    snapshot = _status_registry_snapshot(webspace_id=webspace_id, now=now)
    records: list[ProjectionRecord] = []
    for card in snapshot.get("cards", []):
        if not isinstance(card, Mapping):
            continue
        card_id = str(card.get("id") or "").strip()
        if requested_ids is not None and card_id not in requested_ids:
            continue
        try:
            records.append(
                write_projection_record(
                    status_card_projection_record(
                        card,
                        webspace_id=webspace_id,
                        access=access or {"audience": "shared", "visibility": "operator"},
                    )
                )
            )
        except ValueError:
            continue
    return {
        "ok": True,
        "accepted": True,
        "webspace_id": str(webspace_id or "").strip() or None,
        "demanded_only": bool(demanded_only),
        "requested_card_ids": sorted(requested_ids) if requested_ids is not None else None,
        "materialized_total": len(records),
        "records": [record.to_dict() for record in records],
        "projection_registry": projection_record_registry_snapshot(webspace_id=webspace_id),
        "updated_at": float(now if now is not None else time.time()),
    }


def platform_emitter_contract_snapshot(*, now: float | None = None) -> dict[str, Any]:
    return {
        "contract": "adaos.platform-emitters.status-cards.v1",
        "ready_for_mvp": True,
        "updated_at": float(now if now is not None else time.time()),
        "projection_family": f"{STATUS_CARD_PROJECTION_PREFIX}*",
        "projection_families": [
            f"{STATUS_CARD_PROJECTION_PREFIX}*",
            "platform/notifications",
            "platform/runtime-diagnostics",
        ],
        "existing_status_registry": True,
        "handler": STATUS_CARD_WILDCARD_HANDLER,
        "event_bridge": {
            "topic": "adaos.status.card.changed",
            "dispatcher": "dispatch_demanded_projection_refresh",
            "materializer": "materialize_projection_records_to_yjs",
            "coalesced_by": ["webspace_id", "projection_key"],
        },
        "records": {
            "kind": STATUS_CARD_PROJECTION_KIND,
            "source": "services.status.StatusRegistry",
            "materialize": "materialize_status_card_projection_records",
        },
        "platform_sources": {
            "status_cards": "services.status.StatusRegistry",
            "notifications": "runtime/notifications via existing OperationManager projection",
            "runtime_diagnostics": "platform/nodes/<node_id>/diagnostics reserved branch",
        },
        "boundaries": {
            "status_cards_use_existing_registry": True,
            "projection_records_are_core_owned": True,
            "browser_writes_projection_records": False,
            "skills_publish_status_cards_via_sdk": True,
            "platform_nodes_branch_reserved": True,
        },
    }


__all__ = [
    "STATUS_CARD_PROJECTION_KIND",
    "STATUS_CARD_WILDCARD_HANDLER",
    "ensure_status_card_projection_handler",
    "materialize_status_card_projection_records",
    "platform_emitter_contract_snapshot",
    "refresh_status_card_projection",
    "remove_status_card_projection_handler",
    "status_card_projection_record",
    "status_card_projection_snapshot",
]
