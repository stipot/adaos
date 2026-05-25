from __future__ import annotations

from dataclasses import dataclass
import time
from typing import Any, Mapping
from uuid import uuid4


EVENT_META_KEY = "event"
EVENT_ENVELOPE_ABI_CONTRACT = "adaos.operational-event-envelope.v1"
EVENT_ENVELOPE_META_PATH = "_meta.event"
EVENT_ENVELOPE_REQUIRED_FIELDS = ("type", "source", "ts", "payload")
EVENT_ENVELOPE_METADATA_FIELDS = (
    "event_id",
    "source_authority",
    "actor",
    "scope",
    "trace_id",
    "cause_event_id",
    "schema",
    "version",
    "priority",
)


@dataclass(frozen=True, slots=True)
class EventEnvelope:
    """Shared operational event view compatible with legacy Event objects."""

    type: str
    source: str
    payload: Mapping[str, Any]
    ts: float
    event_id: str | None = None
    source_authority: str | None = None
    actor: Any | None = None
    scope: Any | None = None
    trace_id: str | None = None
    cause_event_id: str | None = None
    schema: str | None = None
    version: str | int | None = None
    priority: str | int | None = None

    def meta(self) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for key in (
            "event_id",
            "source_authority",
            "actor",
            "scope",
            "trace_id",
            "cause_event_id",
            "schema",
            "version",
            "priority",
        ):
            value = getattr(self, key)
            if value is not None:
                out[key] = value
        return out

    def to_dict(self) -> dict[str, Any]:
        data = {
            "event_id": self.event_id,
            "type": self.type,
            "source": self.source,
            "source_authority": self.source_authority,
            "actor": self.actor,
            "scope": self.scope,
            "trace_id": self.trace_id,
            "cause_event_id": self.cause_event_id,
            "schema": self.schema,
            "version": self.version,
            "priority": self.priority,
            "ts": self.ts,
            "payload": self.payload,
        }
        return {key: value for key, value in data.items() if value is not None}


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _payload_from_event(event: Any) -> Mapping[str, Any]:
    payload = getattr(event, "payload", None)
    if payload is None and isinstance(event, Mapping):
        payload = event.get("payload")
    if isinstance(payload, Mapping):
        return payload
    if payload is None:
        return {}
    return {"value": payload}


def _event_value(event: Any, key: str, default: Any = None) -> Any:
    if isinstance(event, Mapping):
        return event.get(key, default)
    return getattr(event, key, default)


def _first_present(*values: Any) -> Any:
    for value in values:
        if value is not None:
            return value
    return None


def _meta_value(nested: Mapping[str, Any], flat: Mapping[str, Any], *keys: str) -> Any:
    for key in keys:
        if key in nested:
            return nested[key]
    for key in keys:
        if key in flat:
            return flat[key]
    return None


def event_payload_meta(payload: Mapping[str, Any] | None) -> Mapping[str, Any]:
    """Return the payload metadata mapping without mutating the payload."""

    return _mapping(_mapping(payload).get("_meta"))


def event_payload_envelope_meta(payload: Mapping[str, Any] | None) -> Mapping[str, Any]:
    """Return nested ``_meta.event`` fields used by the shared envelope ABI."""

    return _mapping(event_payload_meta(payload).get(EVENT_META_KEY))


def enrich_event_payload(
    payload: Mapping[str, Any] | None = None,
    *,
    event_id: str | None = None,
    generate_event_id: bool = False,
    source_authority: str | None = None,
    actor: Any | None = None,
    scope: Any | None = None,
    trace_id: str | None = None,
    cause_event_id: str | None = None,
    schema: str | None = None,
    version: str | int | None = None,
    priority: str | int | None = None,
) -> dict[str, Any]:
    """Copy a payload and attach shared event metadata under ``_meta.event``."""

    data = dict(payload or {})
    meta = dict(_mapping(data.get("_meta")))
    event_meta = dict(_mapping(meta.get(EVENT_META_KEY)))
    if generate_event_id and event_id is None and event_meta.get("event_id") is None:
        event_id = str(uuid4())
    for key, value in {
        "event_id": event_id,
        "source_authority": source_authority,
        "actor": actor,
        "scope": scope,
        "trace_id": trace_id,
        "cause_event_id": cause_event_id,
        "schema": schema,
        "version": version,
        "priority": priority,
    }.items():
        if value is not None:
            event_meta[key] = value
    if event_meta:
        meta[EVENT_META_KEY] = event_meta
    if meta:
        data["_meta"] = meta
    return data


def normalize_event_envelope(
    event: Any,
    *,
    event_id: str | None = None,
    generate_event_id: bool = False,
    source_authority: str | None = None,
    actor: Any | None = None,
    scope: Any | None = None,
    trace_id: str | None = None,
    cause_event_id: str | None = None,
    schema: str | None = None,
    version: str | int | None = None,
    priority: str | int | None = None,
) -> EventEnvelope:
    """Normalize a legacy or enriched event into the shared ABI view."""

    payload = _payload_from_event(event)
    flat_meta = event_payload_meta(payload)
    nested_meta = event_payload_envelope_meta(payload)
    resolved_event_id = _first_present(
        event_id,
        _meta_value(nested_meta, flat_meta, "event_id", "id"),
        _event_value(event, "event_id"),
    )
    if generate_event_id and resolved_event_id is None:
        resolved_event_id = str(uuid4())
    return EventEnvelope(
        event_id=resolved_event_id,
        type=str(_event_value(event, "type", "")),
        source=str(_event_value(event, "source", "")),
        source_authority=_first_present(
            source_authority,
            _meta_value(nested_meta, flat_meta, "source_authority", "authority"),
            _event_value(event, "source_authority"),
        ),
        actor=_first_present(actor, _meta_value(nested_meta, flat_meta, "actor"), _event_value(event, "actor")),
        scope=_first_present(scope, _meta_value(nested_meta, flat_meta, "scope"), _event_value(event, "scope")),
        trace_id=_first_present(trace_id, _meta_value(nested_meta, flat_meta, "trace_id"), _event_value(event, "trace_id")),
        cause_event_id=_first_present(
            cause_event_id,
            _meta_value(nested_meta, flat_meta, "cause_event_id", "cause"),
            _event_value(event, "cause_event_id"),
        ),
        schema=_first_present(schema, _meta_value(nested_meta, flat_meta, "schema"), _event_value(event, "schema")),
        version=_first_present(version, _meta_value(nested_meta, flat_meta, "version"), _event_value(event, "version")),
        priority=_first_present(priority, _meta_value(nested_meta, flat_meta, "priority"), _event_value(event, "priority")),
        ts=float(_event_value(event, "ts", time.time()) or 0.0),
        payload=payload,
    )


def event_envelope_contract_snapshot(*, now: float | None = None) -> dict[str, Any]:
    """Return the shared event envelope ABI as an inspectable contract."""

    sample_payload = enrich_event_payload(
        {"state": "ready"},
        event_id="evt-demo-1",
        trace_id="trace-demo-1",
        source_authority="platform",
        actor={"kind": "system"},
        scope={"webspace_id": "desktop", "node_id": "node-a"},
        schema="node.status",
        version=1,
        priority="normal",
    )
    sample_event = {
        "type": "node.status",
        "source": "runtime",
        "ts": float(now if now is not None else 0.0),
        "payload": sample_payload,
    }
    normalized = normalize_event_envelope(sample_event).to_dict()
    return {
        "contract": EVENT_ENVELOPE_ABI_CONTRACT,
        "ready_for_mvp": True,
        "meta_path": EVENT_ENVELOPE_META_PATH,
        "required_fields": list(EVENT_ENVELOPE_REQUIRED_FIELDS),
        "metadata_fields": list(EVENT_ENVELOPE_METADATA_FIELDS),
        "compatibility": {
            "legacy_event_supported": True,
            "flat_meta_supported": True,
            "nested_meta_preferred": True,
            "payload_copy_on_enrich": True,
        },
        "ownership": {
            "core_owned": [
                "normalization",
                "dispatcher input view",
                "trace and scope propagation",
            ],
            "producer_owned": [
                "event type",
                "payload data",
                "source authority when known",
            ],
            "forbidden": [
                "mutating original payload during enrichment",
                "bypassing shared normalization before dispatch",
            ],
        },
        "sample_event": sample_event,
        "normalized_example": normalized,
        "dispatcher_ready": (
            normalized.get("event_id") == "evt-demo-1"
            and normalized.get("trace_id") == "trace-demo-1"
            and normalized.get("scope", {}).get("webspace_id") == "desktop"
        ),
    }
