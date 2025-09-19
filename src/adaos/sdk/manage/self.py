"""Self-service control-plane tools scoped to the active skill."""

from __future__ import annotations

from typing import Any, Mapping

from adaos.sdk.core.types import SCHEMA_VALIDATION_REPORT
from adaos.sdk.core.validation.skill import validate_self as _validate_self
from adaos.sdk.core.decorators import tool

from .common import (
    SCHEMA_RESULT_ENVELOPE,
    _current_skill_id,
    _load_request,
    _require_cap,
    _result,
    _safe_ns_key,
    _save_request,
)

__all__ = [
    "validate",
    "state_get",
    "state_put",
    "request_update",
]


_VALIDATE_INPUT = {
    "type": "object",
    "properties": {
        "strict": {"type": "boolean", "default": False},
        "probe_tools": {"type": "boolean", "default": False},
    },
    "additionalProperties": False,
}

_STATE_GET_INPUT = {
    "type": "object",
    "properties": {
        "key": {"type": "string", "minLength": 1},
        "default": {
            "type": ["object", "array", "string", "number", "boolean", "null"],
            "description": "Value returned when key is absent.",
        },
    },
    "required": ["key"],
    "additionalProperties": False,
}

_STATE_GET_OUTPUT = {
    "type": "object",
    "properties": {
        "key": {"type": "string"},
        "value": {"type": ["object", "array", "string", "number", "boolean", "null"]},
    },
    "required": ["key"],
    "additionalProperties": True,
}

_STATE_PUT_INPUT = {
    "type": "object",
    "properties": {
        "request_id": {"type": "string", "minLength": 1},
        "dry_run": {"type": "boolean", "default": False},
        "key": {"type": "string", "minLength": 1},
        "value": {"type": ["object", "array", "string", "number", "boolean", "null"]},
    },
    "required": ["request_id", "key", "value"],
    "additionalProperties": True,
}

_UPDATE_REQUEST_INPUT = {
    "type": "object",
    "properties": {
        "request_id": {"type": "string", "minLength": 1},
        "dry_run": {"type": "boolean", "default": False},
        "note": {"type": "string"},
    },
    "required": ["request_id"],
    "additionalProperties": True,
}


@tool(
    "manage.self.validate",
    summary="validate the currently active skill",
    stability="stable",
    examples=["manage.self.validate()"],
    input_schema=_VALIDATE_INPUT,
    output_schema=SCHEMA_VALIDATION_REPORT,
)
def validate(strict: bool = False, probe_tools: bool = False) -> Mapping[str, Any]:
    report = _validate_self(strict=strict, probe_tools=probe_tools)
    issues = [
        {
            "level": issue.level,
            "code": issue.code,
            "message": issue.message,
            "where": getattr(issue, "where", None),
        }
        for issue in report.issues
    ]
    return {"ok": report.ok, "issues": issues}


@tool(
    "manage.self.state.get",
    summary="read a value from the skill-scoped state store",
    stability="stable",
    examples=["manage.self.state.get(key='profile')"],
    input_schema=_STATE_GET_INPUT,
    output_schema=_STATE_GET_OUTPUT,
)
def state_get(key: str, default: Any | None = None) -> Mapping[str, Any]:
    ctx = _require_cap("manage.self")
    skill_id = _current_skill_id()
    storage_key = _safe_ns_key("skills", skill_id, "state", key)
    value = ctx.kv.get(storage_key, default)
    return {"key": key, "value": value}


@tool(
    "manage.self.state.put",
    summary="persist a value into the skill-scoped state store",
    stability="stable",
    idempotent=True,
    examples=["manage.self.state.put(request_id='req-1', key='profile', value={'lang': 'en'})"],
    input_schema=_STATE_PUT_INPUT,
    output_schema=SCHEMA_RESULT_ENVELOPE,
)
def state_put(request_id: str, key: str, value: Any, *, dry_run: bool = False) -> Mapping[str, Any]:
    ctx = _require_cap("manage.self")
    skill_id = _current_skill_id()
    namespace = _safe_ns_key("skills", skill_id, "state")
    request_ns = _safe_ns_key(namespace, "requests")

    cached = _load_request(ctx, request_ns, request_id)
    if cached is not None:
        return dict(cached)

    if not dry_run:
        ctx.kv.set(_safe_ns_key(namespace, key), value)
    envelope = _result(ctx, request_id, "dry-run" if dry_run else "ok", dry_run=dry_run, result={"key": key, "value": value})
    return _save_request(ctx, request_ns, request_id, envelope)


@tool(
    "manage.self.update.request",
    summary="request runtime to refresh or pull the current skill",
    stability="experimental",
    idempotent=True,
    examples=["manage.self.update.request(request_id='sync-1')"],
    input_schema=_UPDATE_REQUEST_INPUT,
    output_schema=SCHEMA_RESULT_ENVELOPE,
)
def request_update(request_id: str, *, dry_run: bool = False, note: str | None = None) -> Mapping[str, Any]:
    ctx = _require_cap("manage.self")
    skill_id = _current_skill_id()
    namespace = _safe_ns_key("skills", skill_id, "requests", "update")

    cached = _load_request(ctx, namespace, request_id)
    if cached is not None:
        return dict(cached)

    status = "dry-run" if dry_run else "pending"
    result_payload: dict[str, Any] = {"skill_id": skill_id, "status": status}
    if note:
        result_payload["note"] = note

    if not dry_run:
        marker_key = _safe_ns_key("skills", skill_id, "update", "desired")
        ctx.kv.set(marker_key, {"request_id": request_id, "note": note})

    envelope = _result(ctx, request_id, status, dry_run=dry_run, result=result_payload)
    return _save_request(ctx, namespace, request_id, envelope)
