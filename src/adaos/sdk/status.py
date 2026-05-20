from __future__ import annotations

from typing import Any, Iterable, Mapping

from adaos.sdk.data.context import get_current_skill
from adaos.sdk.io.out import stream_variable_publish
from adaos.services.agent_context import get_ctx
from adaos.services.eventbus import emit
from adaos.services.status.cards import StatusCard, normalize_status_card

__all__ = ["publish_status", "publish_status_many", "publish_status_stream"]


def _current_owner(owner: str | None = None) -> str:
    token = str(owner or "").strip()
    if token:
        return token
    try:
        current = get_current_skill()
        skill_name = str(getattr(current, "name", "") or "").strip()
    except Exception:
        skill_name = ""
    return f"skill:{skill_name}" if skill_name else "sdk:status"


def _emit_status(topic: str, payload: dict[str, Any]) -> None:
    ctx = get_ctx()
    bus = getattr(ctx, "bus", None)
    if bus is None:
        raise RuntimeError("AgentContext.bus is not initialized")
    try:
        ctx.status_registry
    except Exception:
        pass
    emit(bus, topic, payload, "sdk.status")


def _card_from_args(
    *,
    id: str,
    kind: str,
    status: Any,
    scope: str = "skill",
    owner: str | None = None,
    summary: str | None = None,
    severity: str | None = None,
    webspace_id: str | None = None,
    ttl_ms: int | None = None,
    incident_id: str | None = None,
    details_ref: Mapping[str, Any] | None = None,
    route: Mapping[str, Any] | None = None,
    guard_ref: Mapping[str, Any] | None = None,
    metadata: Mapping[str, Any] | None = None,
    updated_at: float | None = None,
) -> StatusCard:
    payload: dict[str, Any] = {
        "id": id,
        "owner": _current_owner(owner),
        "kind": kind,
        "scope": scope,
        "status": status,
        "summary": summary,
        "severity": severity,
        "webspace_id": webspace_id,
        "ttl_ms": ttl_ms,
        "incident_id": incident_id,
        "details_ref": dict(details_ref or {}),
        "route": dict(route or {}),
        "guard_ref": dict(guard_ref or {}),
        "metadata": dict(metadata or {}),
    }
    if updated_at is not None:
        payload["updated_at"] = float(updated_at)
    return normalize_status_card(payload)


def publish_status(
    *,
    id: str,
    kind: str,
    status: Any,
    scope: str = "skill",
    owner: str | None = None,
    summary: str | None = None,
    severity: str | None = None,
    webspace_id: str | None = None,
    ttl_ms: int | None = None,
    incident_id: str | None = None,
    details_ref: Mapping[str, Any] | None = None,
    route: Mapping[str, Any] | None = None,
    guard_ref: Mapping[str, Any] | None = None,
    metadata: Mapping[str, Any] | None = None,
    updated_at: float | None = None,
    _meta: Mapping[str, Any] | None = None,
) -> Mapping[str, Any]:
    card = _card_from_args(
        id=id,
        kind=kind,
        status=status,
        scope=scope,
        owner=owner,
        summary=summary,
        severity=severity,
        webspace_id=webspace_id,
        ttl_ms=ttl_ms,
        incident_id=incident_id,
        details_ref=details_ref,
        route=route,
        guard_ref=guard_ref,
        metadata=metadata,
        updated_at=updated_at,
    )
    payload: dict[str, Any] = {"card": card.to_dict()}
    if _meta:
        payload["_meta"] = dict(_meta)
    _emit_status("adaos.status.card.single", payload)
    return {"ok": True, "card": card.to_dict()}


def publish_status_many(
    cards: Iterable[StatusCard | Mapping[str, Any]],
    *,
    owner: str | None = None,
    scope: str | None = None,
    _meta: Mapping[str, Any] | None = None,
) -> Mapping[str, Any]:
    defaults: dict[str, Any] = {"owner": _current_owner(owner)}
    if scope:
        defaults["scope"] = scope
    else:
        defaults["scope"] = "skill"
    normalized = [normalize_status_card(card, **defaults).to_dict() for card in cards]
    payload: dict[str, Any] = {"cards": normalized}
    if _meta:
        payload["_meta"] = dict(_meta)
    _emit_status("adaos.status.card.batch", payload)
    return {"ok": True, "published": len(normalized), "cards": normalized}


def publish_status_stream(
    receiver: str,
    *,
    id: str,
    kind: str,
    status: Any,
    scope: str = "skill",
    owner: str | None = None,
    summary: str | None = None,
    severity: str | None = None,
    webspace_id: str | None = None,
    ttl_ms: int | None = None,
    incident_id: str | None = None,
    details_ref: Mapping[str, Any] | None = None,
    route: Mapping[str, Any] | None = None,
    guard_ref: Mapping[str, Any] | None = None,
    metadata: Mapping[str, Any] | None = None,
    updated_at: float | None = None,
    seq: int | None = None,
    _meta: Mapping[str, Any] | None = None,
) -> Mapping[str, Any]:
    receiver_id = str(receiver or "").strip()
    if not receiver_id:
        return {"ok": False}
    card = _card_from_args(
        id=id,
        kind=kind,
        status=status,
        scope=scope,
        owner=owner,
        summary=summary,
        severity=severity,
        webspace_id=webspace_id,
        ttl_ms=ttl_ms,
        incident_id=incident_id,
        details_ref=details_ref or {"kind": "stream", "receiver": receiver_id},
        route=route or {"kind": "stream", "receiver": receiver_id, "snapshot_policy": "on_subscribe"},
        guard_ref=guard_ref,
        metadata=metadata,
        updated_at=updated_at,
    )
    publish_status_many([card], _meta=_meta)
    stream_result = stream_variable_publish(
        receiver_id,
        card.to_dict(),
        var_id=card.id,
        seq=seq,
        updated_at=card.updated_at,
        ttl_ms=card.ttl_ms,
        _meta=_meta,
    )
    return {"ok": bool(stream_result.get("ok")), "card": card.to_dict(), "stream": dict(stream_result)}
