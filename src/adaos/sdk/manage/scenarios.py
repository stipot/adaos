"""Soft control tools for managing scenarios."""

from __future__ import annotations

from typing import Any, Mapping

from adaos.sdk.core.decorators import tool

from .common import (
    SCHEMA_RESULT_ENVELOPE,
    _load_request,
    _require_cap,
    _result,
    _safe_ns_key,
    _save_request,
)

__all__ = ["toggle", "set_binding"]


_TOGGLE_INPUT = {
    "type": "object",
    "properties": {
        "request_id": {"type": "string", "minLength": 1},
        "scenario": {"type": "string", "minLength": 1},
        "enabled": {"type": "boolean"},
        "dry_run": {"type": "boolean", "default": False},
    },
    "required": ["request_id", "scenario", "enabled"],
    "additionalProperties": True,
}

_BIND_INPUT = {
    "type": "object",
    "properties": {
        "request_id": {"type": "string", "minLength": 1},
        "scenario": {"type": "string", "minLength": 1},
        "binding": {"type": "object"},
        "dry_run": {"type": "boolean", "default": False},
    },
    "required": ["request_id", "scenario", "binding"],
    "additionalProperties": True,
}


@tool(
    "scenarios.toggle",
    summary="enable or disable a scenario",
    stability="stable",
    idempotent=True,
    examples=["scenarios.toggle(request_id='req-10', scenario='onboarding', enabled=True)"] ,
    input_schema=_TOGGLE_INPUT,
    output_schema=SCHEMA_RESULT_ENVELOPE,
)
def toggle(request_id: str, scenario: str, *, enabled: bool, dry_run: bool = False) -> Mapping[str, Any]:
    ctx = _require_cap("scenarios.manage")
    namespace = _safe_ns_key("scenarios", scenario, "toggle")

    cached = _load_request(ctx, namespace, request_id)
    if cached is not None:
        return dict(cached)

    if not dry_run:
        key = _safe_ns_key("scenarios", scenario, "enabled")
        ctx.kv.set(key, bool(enabled))
    envelope = _result(ctx, request_id, "dry-run" if dry_run else "ok", dry_run=dry_run, result={"scenario": scenario, "enabled": enabled})
    return _save_request(ctx, namespace, request_id, envelope)


@tool(
    "scenarios.bind.set",
    summary="set scenario binding metadata",
    stability="experimental",
    idempotent=True,
    examples=["scenarios.bind.set(request_id='req-11', scenario='onboarding', binding={'skill': 'welcome'})"],
    input_schema=_BIND_INPUT,
    output_schema=SCHEMA_RESULT_ENVELOPE,
)
def set_binding(request_id: str, scenario: str, binding: Mapping[str, Any], *, dry_run: bool = False) -> Mapping[str, Any]:
    ctx = _require_cap("scenarios.manage")
    namespace = _safe_ns_key("scenarios", scenario, "binding")

    cached = _load_request(ctx, namespace, request_id)
    if cached is not None:
        return dict(cached)

    if not dry_run:
        key = _safe_ns_key("scenarios", scenario, "binding")
        ctx.kv.set(key, dict(binding))
    envelope = _result(ctx, request_id, "dry-run" if dry_run else "ok", dry_run=dry_run, result={"scenario": scenario, "binding": dict(binding)})
    return _save_request(ctx, namespace, request_id, envelope)
