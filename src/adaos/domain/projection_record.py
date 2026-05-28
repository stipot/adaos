from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import hashlib
import json
import time
from typing import Any, Mapping


PROJECTION_ACCESS_AUDIENCES = {"shared", "owner", "guest", "dev"}


class ProjectionStatus(str, Enum):
    READY = "ready"
    LOADING = "loading"
    STALE = "stale"
    ERROR = "error"
    UNAVAILABLE = "unavailable"


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _compact(value: Mapping[str, Any]) -> dict[str, Any]:
    return {str(key): item for key, item in value.items() if item is not None}


def _coerce_bool(value: Any, *, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    token = str(value).strip().lower()
    if token in {"1", "true", "yes", "on"}:
        return True
    if token in {"0", "false", "no", "off"}:
        return False
    return bool(value)


def _coerce_actions(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value] if value.strip() else []
    try:
        return [str(item).strip() for item in value if str(item).strip()]
    except TypeError:
        token = str(value).strip()
        return [token] if token else []


def normalize_projection_access_metadata(
    access: Mapping[str, Any] | None = None,
    *,
    audience: str | None = None,
    read_only: bool | None = None,
    sensitive: bool | None = None,
    actions_allowed: Any = None,
    display_hints: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Return the MVP projection access metadata shape.

    The payload remains shared; clients use this metadata to adjust available
    actions and presentation for owner, guest, and dev audiences.
    """

    base = dict(access) if isinstance(access, Mapping) else {}
    audience_token = str(audience if audience is not None else base.get("audience") or "shared").strip().lower()
    if audience_token not in PROJECTION_ACCESS_AUDIENCES:
        audience_token = "shared"
    hints = _mapping(display_hints if display_hints is not None else base.get("display_hints"))
    return {
        **base,
        "audience": audience_token,
        "read_only": _coerce_bool(read_only if read_only is not None else base.get("read_only"), default=False),
        "sensitive": _coerce_bool(sensitive if sensitive is not None else base.get("sensitive"), default=False),
        "actions_allowed": _coerce_actions(
            actions_allowed if actions_allowed is not None else base.get("actions_allowed")
        ),
        "display_hints": dict(hints),
    }


def _json_default(value: Any) -> str:
    return str(value)


def projection_fingerprint(data: Any) -> str:
    """Return a stable fingerprint for JSON-like projection payloads."""

    raw = json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=_json_default)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class ProjectionMeta:
    projection_key: str
    kind: str
    webspace_id: str
    node_id: str | None = None
    version: int | str | None = None
    fingerprint: str | None = None
    updated_at: float | None = None
    changed_at: float | None = None
    source: str | None = None
    source_authority: str | None = None
    access: Mapping[str, Any] | None = None
    lifecycle_reason: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return _compact(
            {
                "projection_key": self.projection_key,
                "kind": self.kind,
                "webspace_id": self.webspace_id,
                "node_id": self.node_id,
                "version": self.version,
                "fingerprint": self.fingerprint,
                "updated_at": self.updated_at,
                "changed_at": self.changed_at,
                "source": self.source,
                "source_authority": self.source_authority,
                "access": dict(self.access) if isinstance(self.access, Mapping) else self.access,
                "lifecycle_reason": self.lifecycle_reason,
            }
        )


@dataclass(frozen=True, slots=True)
class ProjectionRecord:
    status: str
    data: Any
    meta: ProjectionMeta
    error: Mapping[str, Any] | str | None = None

    def to_dict(self) -> dict[str, Any]:
        return _compact(
            {
                "status": self.status,
                "data": self.data,
                "meta": self.meta.to_dict(),
                "error": dict(self.error) if isinstance(self.error, Mapping) else self.error,
            }
        )


def _coerce_status(value: Any) -> str:
    if isinstance(value, ProjectionStatus):
        return value.value
    token = str(value or "").strip().lower()
    return token or ProjectionStatus.READY.value


def _coerce_previous_meta(previous: Mapping[str, Any] | ProjectionRecord | None) -> Mapping[str, Any]:
    if isinstance(previous, ProjectionRecord):
        return previous.meta.to_dict()
    return _mapping(_mapping(previous).get("meta"))


def _resolve_changed_at(
    *,
    fingerprint: str,
    updated_at: float,
    previous_meta: Mapping[str, Any],
    changed_at: float | None,
) -> float:
    if changed_at is not None:
        return float(changed_at)
    if previous_meta.get("fingerprint") == fingerprint and previous_meta.get("changed_at") is not None:
        try:
            return float(previous_meta["changed_at"])
        except Exception:
            pass
    return updated_at


def _resolve_version(
    *,
    fingerprint: str,
    previous_meta: Mapping[str, Any],
    version: int | str | None,
) -> int | str:
    if version is not None:
        return version
    previous_version = previous_meta.get("version")
    if previous_meta.get("fingerprint") == fingerprint and previous_version is not None:
        return previous_version
    if isinstance(previous_version, int):
        return previous_version + 1
    return 1


def make_projection_record(
    *,
    projection_key: str,
    kind: str,
    data: Any,
    webspace_id: str,
    status: str | ProjectionStatus = ProjectionStatus.READY,
    node_id: str | None = None,
    version: int | str | None = None,
    fingerprint: str | None = None,
    source: str | None = None,
    source_authority: str | None = None,
    access: Mapping[str, Any] | None = None,
    lifecycle_reason: str | None = None,
    error: Mapping[str, Any] | str | None = None,
    previous: Mapping[str, Any] | ProjectionRecord | None = None,
    updated_at: float | None = None,
    changed_at: float | None = None,
) -> ProjectionRecord:
    """Build the canonical projection record used by runtime projection writers."""

    ts = float(updated_at if updated_at is not None else time.time())
    fingerprint = str(fingerprint or projection_fingerprint(data))
    previous_meta = _coerce_previous_meta(previous)
    meta = ProjectionMeta(
        projection_key=str(projection_key),
        kind=str(kind),
        webspace_id=str(webspace_id),
        node_id=node_id,
        version=_resolve_version(fingerprint=fingerprint, previous_meta=previous_meta, version=version),
        fingerprint=fingerprint,
        updated_at=ts,
        changed_at=_resolve_changed_at(
            fingerprint=fingerprint,
            updated_at=ts,
            previous_meta=previous_meta,
            changed_at=changed_at,
        ),
        source=source,
        source_authority=source_authority,
        access=normalize_projection_access_metadata(access),
        lifecycle_reason=lifecycle_reason,
    )
    return ProjectionRecord(status=_coerce_status(status), data=data, meta=meta, error=error)


def normalize_projection_record(record: Mapping[str, Any] | ProjectionRecord) -> ProjectionRecord:
    """Normalize an existing mapping into the canonical projection record shape."""

    if isinstance(record, ProjectionRecord):
        return record

    meta = _mapping(record.get("meta"))
    data = record.get("data")
    return ProjectionRecord(
        status=_coerce_status(record.get("status")),
        data=data,
        meta=ProjectionMeta(
            projection_key=str(meta.get("projection_key") or ""),
            kind=str(meta.get("kind") or ""),
            webspace_id=str(meta.get("webspace_id") or ""),
            node_id=meta.get("node_id"),
            version=meta.get("version"),
            fingerprint=meta.get("fingerprint"),
            updated_at=meta.get("updated_at"),
            changed_at=meta.get("changed_at"),
            source=meta.get("source"),
            source_authority=meta.get("source_authority"),
            access=normalize_projection_access_metadata(_mapping(meta.get("access"))),
            lifecycle_reason=meta.get("lifecycle_reason"),
        ),
        error=record.get("error"),
    )


__all__ = [
    "ProjectionMeta",
    "ProjectionRecord",
    "ProjectionStatus",
    "make_projection_record",
    "normalize_projection_record",
    "normalize_projection_access_metadata",
    "projection_fingerprint",
]
