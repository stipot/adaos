from __future__ import annotations

from dataclasses import dataclass
import time
from typing import Any, Mapping

from .projection_keys import status_card_projection_key
from .projection_record import ProjectionRecord, ProjectionStatus, make_projection_record, projection_fingerprint


STATUS_CARD_PROJECTION_KIND = "status-card"


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _compact(value: Mapping[str, Any]) -> dict[str, Any]:
    return {str(key): item for key, item in value.items() if item is not None}


def normalize_status_card_status(value: Any) -> str:
    token = str(value or "").strip().lower()
    if token in {"online", "up", "ready", "healthy", "active", "ok", "running", "nominal"}:
        return "online"
    if token in {"offline", "down", "failed", "broken", "disconnected", "unreachable", "error"}:
        return "offline"
    if token in {"degraded", "unstable", "limited", "partial"}:
        return "degraded"
    if token in {"warning", "warn", "pending", "pending_update", "draining", "throttled", "overloaded", "stale"}:
        return "warning"
    return "unknown"


def default_status_card_severity(status: Any) -> str:
    token = normalize_status_card_status(status)
    if token == "offline":
        return "critical"
    if token == "degraded":
        return "high"
    if token == "warning":
        return "medium"
    if token == "online":
        return "low"
    return "info"


@dataclass(frozen=True, slots=True)
class StatusCardDetailsRef:
    kind: str
    receiver: str | None = None
    path: str | None = None
    tool: str | None = None
    params: Mapping[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return _compact(
            {
                "kind": self.kind,
                "receiver": self.receiver,
                "path": self.path,
                "tool": self.tool,
                "params": dict(self.params) if isinstance(self.params, Mapping) else self.params,
            }
        )


@dataclass(frozen=True, slots=True)
class StatusCard:
    id: str
    owner: str
    kind: str
    scope: Any
    status: str
    summary: str
    severity: str
    updated_at: float
    ttl_ms: int | None = None
    webspace_id: str | None = None
    version: int | str | None = None
    fingerprint: str | None = None
    changed_at: float | None = None
    details_ref: StatusCardDetailsRef | None = None
    incident_id: str | None = None

    def fingerprint_dict(self) -> dict[str, Any]:
        return _compact(
            {
                "id": self.id,
                "owner": self.owner,
                "kind": self.kind,
                "scope": self.scope,
                "webspace_id": self.webspace_id,
                "status": self.status,
                "summary": self.summary,
                "severity": self.severity,
                "ttl_ms": self.ttl_ms,
                "details_ref": self.details_ref.to_dict() if self.details_ref else None,
                "incident_id": self.incident_id,
            }
        )

    def content_dict(self) -> dict[str, Any]:
        return _compact(
            {
                "id": self.id,
                "owner": self.owner,
                "kind": self.kind,
                "scope": self.scope,
                "webspace_id": self.webspace_id,
                "status": self.status,
                "summary": self.summary,
                "severity": self.severity,
                "updated_at": self.updated_at,
                "ttl_ms": self.ttl_ms,
                "details_ref": self.details_ref.to_dict() if self.details_ref else None,
                "incident_id": self.incident_id,
            }
        )

    def to_dict(self) -> dict[str, Any]:
        data = self.content_dict()
        data.update(
            _compact(
                {
                    "version": self.version,
                    "fingerprint": self.fingerprint,
                    "changed_at": self.changed_at,
                }
            )
        )
        return data


def _previous_card_meta(previous: Mapping[str, Any] | ProjectionRecord | StatusCard | None) -> Mapping[str, Any]:
    if isinstance(previous, StatusCard):
        return _compact(
            {
                "fingerprint": previous.fingerprint,
                "version": previous.version,
                "changed_at": previous.changed_at,
            }
        )
    if isinstance(previous, ProjectionRecord):
        return previous.meta.to_dict()
    data = _mapping(previous)
    return _mapping(data.get("meta")) or data


def _resolve_version(fingerprint: str, previous_meta: Mapping[str, Any], version: int | str | None) -> int | str:
    if version is not None:
        return version
    previous_version = previous_meta.get("version")
    if previous_meta.get("fingerprint") == fingerprint and previous_version is not None:
        return previous_version
    if isinstance(previous_version, int):
        return previous_version + 1
    return 1


def _resolve_changed_at(fingerprint: str, previous_meta: Mapping[str, Any], updated_at: float) -> float:
    if previous_meta.get("fingerprint") == fingerprint and previous_meta.get("changed_at") is not None:
        try:
            return float(previous_meta["changed_at"])
        except Exception:
            pass
    return updated_at


def make_status_card(
    *,
    id: str,
    owner: str,
    kind: str,
    scope: Any,
    status: Any,
    summary: str,
    webspace_id: str | None = None,
    severity: str | None = None,
    updated_at: float | None = None,
    ttl_ms: int | None = None,
    details_ref: Mapping[str, Any] | StatusCardDetailsRef | None = None,
    incident_id: str | None = None,
    version: int | str | None = None,
    previous: Mapping[str, Any] | ProjectionRecord | StatusCard | None = None,
) -> StatusCard:
    ts = float(updated_at if updated_at is not None else time.time())
    normalized_status = normalize_status_card_status(status)
    details = normalize_status_card_details_ref(details_ref) if details_ref is not None else None
    base = StatusCard(
        id=str(id),
        owner=str(owner),
        kind=str(kind),
        scope=scope,
        webspace_id=webspace_id,
        status=normalized_status,
        summary=str(summary),
        severity=str(severity or default_status_card_severity(normalized_status)),
        updated_at=ts,
        ttl_ms=ttl_ms,
        details_ref=details,
        incident_id=incident_id,
        )
    fingerprint = projection_fingerprint(base.fingerprint_dict())
    previous_meta = _previous_card_meta(previous)
    return StatusCard(
        id=base.id,
        owner=base.owner,
        kind=base.kind,
        scope=base.scope,
        webspace_id=base.webspace_id,
        status=base.status,
        summary=base.summary,
        severity=base.severity,
        updated_at=base.updated_at,
        ttl_ms=base.ttl_ms,
        details_ref=base.details_ref,
        incident_id=base.incident_id,
        version=_resolve_version(fingerprint, previous_meta, version),
        fingerprint=fingerprint,
        changed_at=_resolve_changed_at(fingerprint, previous_meta, ts),
    )


def normalize_status_card_details_ref(value: Mapping[str, Any] | StatusCardDetailsRef) -> StatusCardDetailsRef:
    if isinstance(value, StatusCardDetailsRef):
        return value
    data = _mapping(value)
    return StatusCardDetailsRef(
        kind=str(data.get("kind") or ""),
        receiver=data.get("receiver"),
        path=data.get("path"),
        tool=data.get("tool"),
        params=_mapping(data.get("params")) or None,
    )


def is_status_card_stale(card: StatusCard | Mapping[str, Any], *, now: float | None = None) -> bool:
    data = card.to_dict() if isinstance(card, StatusCard) else _mapping(card)
    ttl_ms = data.get("ttl_ms")
    if ttl_ms is None:
        return False
    try:
        ttl_s = max(0.0, float(ttl_ms) / 1000.0)
        updated_at = float(data.get("updated_at") or 0.0)
    except Exception:
        return False
    return updated_at + ttl_s < float(now if now is not None else time.time())


def make_status_card_projection_record(
    card: StatusCard,
    *,
    webspace_id: str | None = None,
    node_id: str | None = None,
    source: str | None = None,
    source_authority: str | None = None,
    access: Mapping[str, Any] | None = None,
    status: str | ProjectionStatus = ProjectionStatus.READY,
    lifecycle_reason: str | None = None,
    previous: Mapping[str, Any] | ProjectionRecord | None = None,
) -> ProjectionRecord:
    return make_projection_record(
        projection_key=status_card_projection_key(card.id),
        kind=STATUS_CARD_PROJECTION_KIND,
        data=card.to_dict(),
        webspace_id=str(webspace_id or card.webspace_id or ""),
        status=status,
        node_id=node_id,
        version=card.version,
        fingerprint=card.fingerprint,
        source=source or card.owner,
        source_authority=source_authority,
        access=access,
        lifecycle_reason=lifecycle_reason,
        previous=previous,
        updated_at=card.updated_at,
        changed_at=card.changed_at,
    )


__all__ = [
    "STATUS_CARD_PROJECTION_KIND",
    "StatusCard",
    "StatusCardDetailsRef",
    "default_status_card_severity",
    "is_status_card_stale",
    "make_status_card",
    "make_status_card_projection_record",
    "normalize_status_card_details_ref",
    "normalize_status_card_status",
]
