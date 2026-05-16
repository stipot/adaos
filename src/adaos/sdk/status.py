"""Skill-facing helpers for publishing small operational status cards."""

from __future__ import annotations

import time
from typing import Any, Iterable, Mapping

from adaos.domain import StatusCard
from adaos.sdk.core.errors import SdkRuntimeNotInitialized


def _current_skill_owner() -> str | None:
    try:
        from adaos.sdk.data.context import get_current_skill

        current = get_current_skill()
    except SdkRuntimeNotInitialized:
        raise
    except Exception:
        return None
    name = str(getattr(current, "name", "") or "").strip() if current is not None else ""
    return f"skill:{name}" if name else None


def _resolve_owner(owner: str | None) -> str:
    token = str(owner or "").strip()
    if token:
        return token
    skill_owner = _current_skill_owner()
    if skill_owner:
        return skill_owner
    raise ValueError("owner is required when no current skill context is active")


def _coerce_scope(scope: Mapping[str, Any] | str | None) -> Mapping[str, Any] | str:
    if isinstance(scope, Mapping):
        return dict(scope)
    return scope or {}


def _publish_status_card(**kwargs: Any) -> StatusCard:
    from adaos.services.status_card_registry import ensure_status_card_dispatcher_handler, publish_status_card

    ensure_status_card_dispatcher_handler()
    return publish_status_card(**kwargs)


def publish_status(
    *,
    id: str,
    status: str,
    summary: str,
    webspace_id: str,
    owner: str | None = None,
    kind: str = "skill",
    scope: Mapping[str, Any] | str | None = None,
    severity: str | None = None,
    ttl_ms: int | None = None,
    details_ref: Mapping[str, Any] | None = None,
    incident_id: str | None = None,
    updated_at: float | None = None,
) -> StatusCard:
    """Publish one compact status card without touching Yjs directly."""

    return _publish_status_card(
        id=id,
        owner=_resolve_owner(owner),
        kind=kind,
        scope=_coerce_scope(scope),
        webspace_id=webspace_id,
        status=status,
        summary=summary,
        severity=severity,
        ttl_ms=ttl_ms,
        details_ref=details_ref,
        incident_id=incident_id,
        updated_at=updated_at if updated_at is not None else time.time(),
    )


def publish_status_many(
    cards: Iterable[Mapping[str, Any]],
    *,
    webspace_id: str | None = None,
    owner: str | None = None,
    kind: str = "skill",
    updated_at: float | None = None,
) -> list[StatusCard]:
    """Publish a small batch of status cards with shared defaults."""

    published: list[StatusCard] = []
    batch_ts = updated_at if updated_at is not None else time.time()
    for item in cards:
        data = dict(item)
        card_webspace_id = str(data.pop("webspace_id", webspace_id or "") or "").strip()
        if not card_webspace_id:
            raise ValueError("webspace_id is required")
        published.append(
            publish_status(
                id=str(data.pop("id")),
                owner=data.pop("owner", owner),
                kind=str(data.pop("kind", kind)),
                webspace_id=card_webspace_id,
                status=str(data.pop("status")),
                summary=str(data.pop("summary")),
                scope=data.pop("scope", None),
                severity=data.pop("severity", None),
                ttl_ms=data.pop("ttl_ms", None),
                details_ref=data.pop("details_ref", None),
                incident_id=data.pop("incident_id", None),
                updated_at=data.pop("updated_at", batch_ts),
            )
        )
    return published


def publish_status_stream(
    *,
    id: str,
    status: str,
    summary: str,
    webspace_id: str,
    receiver: str,
    owner: str | None = None,
    kind: str = "skill",
    scope: Mapping[str, Any] | str | None = None,
    path: str | None = None,
    tool: str | None = None,
    params: Mapping[str, Any] | None = None,
    severity: str | None = None,
    ttl_ms: int | None = None,
    incident_id: str | None = None,
    updated_at: float | None = None,
) -> StatusCard:
    """Publish a status card whose details are served by an existing stream/tool."""

    details_ref = {
        "kind": "stream",
        "receiver": receiver,
        "path": path,
        "tool": tool,
        "params": dict(params) if isinstance(params, Mapping) else params,
    }
    return publish_status(
        id=id,
        owner=owner,
        kind=kind,
        scope=scope,
        webspace_id=webspace_id,
        status=status,
        summary=summary,
        severity=severity,
        ttl_ms=ttl_ms,
        details_ref=details_ref,
        incident_id=incident_id,
        updated_at=updated_at,
    )


__all__ = [
    "publish_status",
    "publish_status_many",
    "publish_status_stream",
]
