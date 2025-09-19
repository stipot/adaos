"""Ticketing helpers for requesting external resources."""

from __future__ import annotations

from typing import Any, Mapping

from adaos.sdk.core.decorators import tool
from adaos.sdk.core.errors import SdkRuntimeNotInitialized

from .common import (
    SCHEMA_RESULT_ENVELOPE,
    _current_skill_id,
    _load_request,
    _require_cap,
    _result,
    _safe_ns_key,
    _save_request,
)

__all__ = ["request", "status"]


_REQUEST_INPUT = {
    "type": "object",
    "properties": {
        "request_id": {"type": "string", "minLength": 1},
        "category": {"type": "string", "minLength": 1},
        "details": {"type": "object"},
        "dry_run": {"type": "boolean", "default": False},
    },
    "required": ["request_id", "category"],
    "additionalProperties": True,
}

_STATUS_INPUT = {
    "type": "object",
    "properties": {
        "request_id": {"type": "string", "minLength": 1},
    },
    "required": ["request_id"],
    "additionalProperties": False,
}

_STATUS_OUTPUT = {
    "type": "object",
    "properties": {
        "request_id": {"type": "string"},
        "status": {"type": "string"},
        "details": {"type": ["object", "null"]},
    },
    "required": ["request_id", "status"],
    "additionalProperties": True,
}


@tool(
    "resources.request",
    summary="create a resource ticket for operators",
    stability="experimental",
    idempotent=True,
    examples=["resources.request(request_id='ticket-1', category='hardware', details={'cpu': 'gpu'})"],
    input_schema=_REQUEST_INPUT,
    output_schema=SCHEMA_RESULT_ENVELOPE,
)
def request(request_id: str, category: str, *, details: Mapping[str, Any] | None = None, dry_run: bool = False) -> Mapping[str, Any]:
    ctx = _require_cap("resources.manage")
    try:
        skill_id = _current_skill_id()
    except SdkRuntimeNotInitialized:
        skill_id = None
    namespaces = []
    if skill_id:
        namespaces.append(_safe_ns_key("skills", skill_id, "resources", "requests"))
    namespaces.append(_safe_ns_key("resources", "requests"))

    cached = None
    for namespace in namespaces:
        cached = _load_request(ctx, namespace, request_id)
        if cached is not None:
            break
    if cached is not None:
        if dry_run:
            return dict(cached)
        if str(cached.get("status")) != "dry-run":
            return dict(cached)
        cached = None

    status = "dry-run" if dry_run else "pending"
    payload: dict[str, Any] = {"request_id": request_id, "status": status, "category": category}
    if details:
        payload["details"] = dict(details)

    kv_key = _safe_ns_key("resources", "requests", request_id)
    if skill_id:
        kv_key = _safe_ns_key("skills", skill_id, "resources", "requests", request_id)

    if not dry_run:
        ctx.kv.set(kv_key, payload)

    envelope = _result(ctx, request_id, status, dry_run=dry_run, result=payload)
    if dry_run:
        return envelope
    namespace = namespaces[0]
    return _save_request(ctx, namespace, request_id, envelope)


@tool(
    "resources.status",
    summary="lookup the status of a resource request",
    stability="experimental",
    examples=["resources.status(request_id='ticket-1')"],
    input_schema=_STATUS_INPUT,
    output_schema=_STATUS_OUTPUT,
)
def status(request_id: str) -> Mapping[str, Any]:
    ctx = _require_cap("resources.manage")
    try:
        skill_id = _current_skill_id()
    except SdkRuntimeNotInitialized:
        skill_id = None
    kv_keys = []
    if skill_id:
        kv_keys.append(_safe_ns_key("skills", skill_id, "resources", "requests", request_id))
    kv_keys.append(_safe_ns_key("resources", "requests", request_id))

    payload = None
    for kv_key in kv_keys:
        value = ctx.kv.get(kv_key)
        if isinstance(value, Mapping):
            payload = value
            break
    if not isinstance(payload, Mapping):
        return {"request_id": request_id, "status": "unknown", "details": None}
    result = dict(payload)
    result.setdefault("request_id", request_id)
    result.setdefault("status", "pending")
    return result
