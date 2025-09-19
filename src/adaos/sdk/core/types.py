"""Shared protocol and JSON-schema helpers used across SDK facades."""

from __future__ import annotations

from typing import Any, Awaitable, Callable, Dict, Protocol, TypedDict


Topic = str
Payload = Dict[str, Any]
Handler = Callable[[Payload], Awaitable[Any]]


class ToolFn(Protocol):
    def __call__(self, *args: Any, **kwargs: Any) -> Any: ...


class ResultEnvelope(TypedDict, total=False):
    request_id: str
    status: str
    dry_run: bool
    result: Any
    trace_id: str
    meta: Dict[str, Any]


def result_envelope(
    *,
    request_id: str,
    status: str,
    dry_run: bool,
    result: Any | None = None,
    trace_id: str | None = None,
    meta: Dict[str, Any] | None = None,
) -> ResultEnvelope:
    envelope: ResultEnvelope = {
        "request_id": request_id,
        "status": status,
        "dry_run": dry_run,
    }
    if result is not None:
        envelope["result"] = result
    if trace_id:
        envelope["trace_id"] = trace_id
    if meta:
        envelope["meta"] = meta
    return envelope


SCHEMA_REQUEST_BASE: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "request_id": {"type": "string", "minLength": 1},
        "dry_run": {"type": "boolean", "default": False},
    },
    "required": ["request_id"],
    "additionalProperties": True,
}

SCHEMA_RESULT_ENVELOPE: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "request_id": {"type": "string"},
        "status": {"type": "string"},
        "dry_run": {"type": "boolean"},
        "result": {"type": ["object", "array", "string", "number", "boolean", "null"]},
        "trace_id": {"type": "string"},
        "meta": {"type": "object"},
        "stored_at": {"type": "string", "format": "date-time"},
    },
    "required": ["request_id", "status", "dry_run"],
    "additionalProperties": True,
}

SCHEMA_ISSUE: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "level": {"type": "string"},
        "code": {"type": "string"},
        "message": {"type": "string"},
        "where": {"type": ["string", "null"]},
    },
    "required": ["level", "code", "message"],
    "additionalProperties": True,
}

SCHEMA_VALIDATION_REPORT: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "ok": {"type": "boolean"},
        "issues": {"type": "array", "items": SCHEMA_ISSUE},
    },
    "required": ["ok", "issues"],
    "additionalProperties": True,
}

SCHEMA_SKILL_META: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "id": {"type": "string"},
        "name": {"type": "string"},
        "version": {"type": "string"},
        "path": {"type": "string"},
    },
    "required": ["id", "name", "version", "path"],
    "additionalProperties": True,
}

SCHEMA_SCENARIO_META = SCHEMA_SKILL_META


__all__ = [
    "Topic",
    "Payload",
    "Handler",
    "ToolFn",
    "ResultEnvelope",
    "result_envelope",
    "SCHEMA_REQUEST_BASE",
    "SCHEMA_RESULT_ENVELOPE",
    "SCHEMA_ISSUE",
    "SCHEMA_VALIDATION_REPORT",
    "SCHEMA_SKILL_META",
    "SCHEMA_SCENARIO_META",
]
