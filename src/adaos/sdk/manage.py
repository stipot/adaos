"""Control-plane helpers exposed to skills via the SDK."""

from __future__ import annotations

from typing import Any, Dict, Optional

from adaos.domain import SkillMeta
from adaos.services.resources.requests import ResourceRequestService
from adaos.services.scenario.control import ScenarioControlService
from adaos.services.skill.lifecycle import SkillLifecycleService
from adaos.services.skill.state import SkillStateService, _validate_key as _validate_state_key
from adaos.services.skill.update import SkillUpdateService
from adaos.services.skill.validation import SkillValidationService

from ._cap import require_cap
from ._idem import load_request, save_request
from .context import get_current_skill
from .decorators import tool
from .errors import CapabilityError, QuotaExceeded


_JSON_ANY = {"type": ["object", "array", "string", "number", "boolean", "null"]}


def _issue_to_dict(issue: Any) -> Dict[str, Any]:
    return {
        "level": getattr(issue, "level", "error"),
        "code": getattr(issue, "code", "unknown"),
        "message": getattr(issue, "message", ""),
        "where": getattr(issue, "where", None),
    }


def _meta_to_dict(meta: SkillMeta | None) -> Dict[str, Any]:
    if meta is None:
        return {"id": None, "name": None, "version": None}
    return {"id": meta.id.value, "name": meta.name, "version": getattr(meta, "version", None)}


def _current_skill_id() -> Optional[str]:
    skill = get_current_skill()
    return skill.name if skill else None


def _wrap_quota(fn):
    try:
        return fn()
    except Exception as exc:  # pragma: no cover - defensive guard
        text = str(exc).lower()
        if "quota" in text or getattr(exc, "quota_exceeded", False):
            raise QuotaExceeded(message=str(exc)) from exc
        raise


@tool(
    "manage.self.validate",
    summary="Validate the current skill against schema and runtime exports",
    stability="stable",
    examples=["manage.self.validate(strict=true)"],
    input_schema={
        "type": "object",
        "properties": {
            "strict": {"type": "boolean", "default": False},
            "probe_tools": {"type": "boolean", "default": False},
        },
        "required": [],
        "additionalProperties": False,
    },
    output_schema={
        "type": "object",
        "properties": {
            "ok": {"type": "boolean"},
            "issues": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "level": {"type": "string"},
                        "code": {"type": "string"},
                        "message": {"type": "string"},
                        "where": {"type": ["string", "null"]},
                    },
                    "required": ["level", "code", "message"],
                    "additionalProperties": False,
                },
            },
        },
        "required": ["ok", "issues"],
        "additionalProperties": False,
    },
)
def self_validate(strict: bool = False, probe_tools: bool = False) -> dict:
    ctx = require_cap("manage.self.validate")
    skill_id = _current_skill_id()
    report = SkillValidationService(ctx).validate(skill_id, strict=strict, probe_tools=probe_tools)
    return {"ok": bool(report.ok), "issues": [_issue_to_dict(issue) for issue in report.issues]}


@tool(
    "manage.self.state.get",
    summary="Get private per-skill state from KV",
    stability="stable",
    examples=["manage.self.state.get('last_run')"],
    input_schema={
        "type": "object",
        "properties": {
            "key": {"type": "string"},
            "default": _JSON_ANY,
        },
        "required": ["key"],
        "additionalProperties": False,
    },
    output_schema={
        "type": "object",
        "properties": {
            "status": {"type": "string"},
            "key": {"type": "string"},
            "value": _JSON_ANY,
            "found": {"type": "boolean"},
        },
        "required": ["status", "key", "value", "found"],
        "additionalProperties": False,
    },
)
def self_state_get(key: str, default: object | None = None) -> dict:
    ctx = require_cap("manage.self.state")
    skill_id = _current_skill_id()
    service = SkillStateService(ctx)
    value, found = service.get(skill_id, key, default)
    return {"status": "ok", "key": key, "value": value, "found": found}


@tool(
    "manage.self.state.put",
    summary="Set private per-skill state in KV",
    stability="stable",
    examples=["manage.self.state.put('last_run', {'ts': 1690000000}, request_id='abc')"],
    input_schema={
        "type": "object",
        "properties": {
            "key": {"type": "string"},
            "value": _JSON_ANY,
            "request_id": {"type": "string"},
            "dry_run": {"type": "boolean", "default": False},
        },
        "required": ["key", "value", "request_id"],
        "additionalProperties": False,
    },
    output_schema={
        "type": "object",
        "properties": {
            "status": {"type": "string"},
            "key": {"type": "string"},
            "value": _JSON_ANY,
            "stored": {"type": "boolean"},
            "dry_run": {"type": "boolean"},
            "request_id": {"type": "string"},
            "previous": _JSON_ANY,
            "previous_exists": {"type": "boolean"},
        },
        "required": ["status", "key", "value", "stored", "dry_run", "request_id", "previous", "previous_exists"],
        "additionalProperties": False,
    },
)
def self_state_put(key: str, value: object, request_id: str, dry_run: bool = False) -> dict:
    if not request_id:
        raise ValueError("request_id must be provided")

    ctx = require_cap("manage.self.state")
    skill_id = _current_skill_id()
    service = SkillStateService(ctx)

    if not dry_run:
        cached = load_request("manage.self.state.put", request_id)
        if cached is not None:
            return cached

    preview_value, preview_exists = service.get(skill_id, key)

    result = {
        "status": "ok",
        "key": key,
        "value": value,
        "stored": False,
        "dry_run": dry_run,
        "request_id": request_id,
        "previous": preview_value,
        "previous_exists": preview_exists,
    }

    if dry_run:
        service.request_key(skill_id, request_id)  # validate request namespace
        return result

    _wrap_quota(lambda: service.set(skill_id, key, value))
    result["stored"] = True
    _wrap_quota(lambda: save_request("manage.self.state.put", request_id, result))
    alias_key = service.request_key(skill_id, request_id)
    _wrap_quota(lambda: ctx.kv.set(alias_key, result))
    return result


@tool(
    "manage.self.update.request",
    summary="Request pulling latest version of the current skill",
    stability="beta",
    examples=["manage.self.update.request(request_id='abc', dry_run=true)"],
    input_schema={
        "type": "object",
        "properties": {
            "request_id": {"type": "string"},
            "dry_run": {"type": "boolean", "default": False},
        },
        "required": ["request_id"],
        "additionalProperties": False,
    },
    output_schema={
        "type": "object",
        "properties": {
            "status": {"type": "string"},
            "updated": {"type": "boolean"},
            "version": {"type": ["string", "null"]},
            "dry_run": {"type": "boolean"},
            "request_id": {"type": "string"},
            "message": {"type": ["string", "null"]},
        },
        "required": ["status", "updated", "dry_run", "request_id"],
        "additionalProperties": False,
    },
)
def self_update_request(request_id: str, dry_run: bool = False) -> dict:
    if not request_id:
        raise ValueError("request_id must be provided")

    ctx = require_cap("manage.self.update")
    skill_id = _current_skill_id()
    if not skill_id:
        return {"status": "error", "code": "skill_not_selected", "updated": False, "dry_run": dry_run, "request_id": request_id}

    if not dry_run:
        cached = load_request("manage.self.update.request", request_id)
        if cached is not None:
            return cached

    service = SkillUpdateService(ctx)
    try:
        update_result = service.request_update(skill_id, dry_run=dry_run)
    except PermissionError as exc:
        raise CapabilityError("manage.self.update") from exc
    except FileNotFoundError as exc:
        return {
            "status": "error",
            "code": "not_found",
            "message": str(exc),
            "updated": False,
            "dry_run": dry_run,
            "request_id": request_id,
        }

    result = {
        "status": "ok",
        "updated": bool(update_result.updated),
        "version": update_result.version,
        "dry_run": dry_run,
        "request_id": request_id,
    }

    if dry_run:
        return result

    _wrap_quota(lambda: save_request("manage.self.update.request", request_id, result))
    return result


@tool(
    "skills.install",
    summary="Install a skill (monorepo name or git url)",
    stability="beta",
    examples=["skills.install('weather_skill', request_id='abc')", "skills.install('https://example/git/foo.git', request_id='abc')"],
    input_schema={
        "type": "object",
        "properties": {
            "ref": {"type": "string"},
            "branch": {"type": ["string", "null"]},
            "dest_name": {"type": ["string", "null"]},
            "request_id": {"type": "string"},
            "dry_run": {"type": "boolean", "default": False},
        },
        "required": ["ref", "request_id"],
        "additionalProperties": False,
    },
    output_schema={
        "type": "object",
        "properties": {
            "status": {"type": "string"},
            "action": {"type": "string"},
            "skill": {
                "type": "object",
                "properties": {
                    "id": {"type": ["string", "null"]},
                    "name": {"type": ["string", "null"]},
                    "version": {"type": ["string", "null"]},
                },
                "required": ["id", "name", "version"],
                "additionalProperties": False,
            },
            "dry_run": {"type": "boolean"},
            "request_id": {"type": "string"},
        },
        "required": ["status", "action", "skill", "dry_run", "request_id"],
        "additionalProperties": False,
    },
)
def skills_install(
    ref: str,
    *,
    branch: str | None = None,
    dest_name: str | None = None,
    request_id: str,
    dry_run: bool = False,
) -> dict:
    if not request_id:
        raise ValueError("request_id must be provided")

    ctx = require_cap("manage.skills.install")
    service = SkillLifecycleService(ctx)
    target_id = dest_name or ref
    repo = ctx.skills_repo
    repo.ensure()
    existing = repo.get(target_id)

    if not dry_run:
        cached = load_request("manage.skills.install", request_id)
        if cached is not None:
            return cached

    if dry_run:
        action = "noop" if existing else "install"
        if existing is None:
            meta_dict = {"id": target_id, "name": target_id, "version": None}
        else:
            meta_dict = _meta_to_dict(existing)
        result = {"status": "ok", "action": action, "skill": meta_dict, "dry_run": True, "request_id": request_id}
        return result

    meta = _wrap_quota(lambda: service.install(ref, branch=branch, dest_name=dest_name))
    result = {
        "status": "ok",
        "action": "install",
        "skill": _meta_to_dict(meta),
        "dry_run": False,
        "request_id": request_id,
    }
    _wrap_quota(lambda: save_request("manage.skills.install", request_id, result))
    return result


@tool(
    "skills.uninstall",
    summary="Uninstall a skill by id",
    stability="beta",
    examples=["skills.uninstall('weather_skill', request_id='xyz')"],
    input_schema={
        "type": "object",
        "properties": {
            "skill_id": {"type": "string"},
            "request_id": {"type": "string"},
            "dry_run": {"type": "boolean", "default": False},
        },
        "required": ["skill_id", "request_id"],
        "additionalProperties": False,
    },
    output_schema={
        "type": "object",
        "properties": {
            "status": {"type": "string"},
            "action": {"type": "string"},
            "skill_id": {"type": "string"},
            "dry_run": {"type": "boolean"},
            "request_id": {"type": "string"},
        },
        "required": ["status", "action", "skill_id", "dry_run", "request_id"],
        "additionalProperties": False,
    },
)
def skills_uninstall(skill_id: str, request_id: str, dry_run: bool = False) -> dict:
    if not request_id:
        raise ValueError("request_id must be provided")

    ctx = require_cap("manage.skills.uninstall")
    service = SkillLifecycleService(ctx)

    if not dry_run:
        cached = load_request("manage.skills.uninstall", request_id)
        if cached is not None:
            return cached

    repo = ctx.skills_repo
    repo.ensure()
    existing = repo.get(skill_id)

    if dry_run:
        action = "removed" if existing else "noop"
        return {
            "status": "ok",
            "action": action,
            "skill_id": skill_id,
            "dry_run": True,
            "request_id": request_id,
        }

    removed = _wrap_quota(lambda: service.uninstall(skill_id))
    result = {
        "status": "ok",
        "action": "removed" if removed else "noop",
        "skill_id": skill_id,
        "dry_run": False,
        "request_id": request_id,
    }
    _wrap_quota(lambda: save_request("manage.skills.uninstall", request_id, result))
    return result


@tool(
    "skills.list",
    summary="List installed skills",
    stability="stable",
    examples=["skills.list()"],
    input_schema={"type": "object", "properties": {}, "required": [], "additionalProperties": False},
    output_schema={
        "type": "object",
        "properties": {
            "skills": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "id": {"type": ["string", "null"]},
                        "name": {"type": ["string", "null"]},
                        "version": {"type": ["string", "null"]},
                    },
                    "required": ["id", "name", "version"],
                    "additionalProperties": False,
                },
            }
        },
        "required": ["skills"],
        "additionalProperties": False,
    },
)
def skills_list() -> dict:
    ctx = require_cap("manage.skills.list")
    service = SkillLifecycleService(ctx)
    metas = service.list_installed()
    return {"skills": [_meta_to_dict(meta) for meta in metas]}


@tool(
    "scenarios.toggle",
    summary="Enable or disable a scenario",
    stability="stable",
    examples=["scenarios.toggle('morning', enabled=true, request_id='req1')"],
    input_schema={
        "type": "object",
        "properties": {
            "scenario_id": {"type": "string"},
            "enabled": {"type": "boolean"},
            "request_id": {"type": "string"},
            "dry_run": {"type": "boolean", "default": False},
        },
        "required": ["scenario_id", "enabled", "request_id"],
        "additionalProperties": False,
    },
    output_schema={
        "type": "object",
        "properties": {
            "status": {"type": "string"},
            "scenario_id": {"type": "string"},
            "enabled": {"type": "boolean"},
            "dry_run": {"type": "boolean"},
            "request_id": {"type": "string"},
        },
        "required": ["status", "scenario_id", "enabled", "dry_run", "request_id"],
        "additionalProperties": False,
    },
)
def scenarios_toggle(scenario_id: str, enabled: bool, request_id: str, dry_run: bool = False) -> dict:
    if not request_id:
        raise ValueError("request_id must be provided")

    ctx = require_cap("manage.scenarios.toggle")

    if not dry_run:
        cached = load_request("manage.scenarios.toggle", request_id)
        if cached is not None:
            return cached

    service = ScenarioControlService(ctx)
    if not dry_run:
        _wrap_quota(lambda: service.set_enabled(scenario_id, enabled))
    result = {
        "status": "ok",
        "scenario_id": scenario_id,
        "enabled": bool(enabled),
        "dry_run": dry_run,
        "request_id": request_id,
    }
    if not dry_run:
        _wrap_quota(lambda: save_request("manage.scenarios.toggle", request_id, result))
    return result


@tool(
    "scenarios.bind.set",
    summary="Set a binding (param/device) for a scenario",
    stability="beta",
    examples=["scenarios.bind.set('alarm', key='mic', value='default', request_id='a1')"],
    input_schema={
        "type": "object",
        "properties": {
            "scenario_id": {"type": "string"},
            "key": {"type": "string"},
            "value": {"type": "string"},
            "request_id": {"type": "string"},
            "dry_run": {"type": "boolean", "default": False},
        },
        "required": ["scenario_id", "key", "value", "request_id"],
        "additionalProperties": False,
    },
    output_schema={
        "type": "object",
        "properties": {
            "status": {"type": "string"},
            "scenario_id": {"type": "string"},
            "key": {"type": "string"},
            "value": {"type": "string"},
            "dry_run": {"type": "boolean"},
            "request_id": {"type": "string"},
            "code": {"type": ["string", "null"]},
        },
        "required": ["status", "scenario_id", "key", "value", "dry_run", "request_id"],
        "additionalProperties": False,
    },
)
def scenarios_bind_set(scenario_id: str, key: str, value: str, request_id: str, dry_run: bool = False) -> dict:
    if not request_id:
        raise ValueError("request_id must be provided")

    ctx = require_cap("manage.scenarios.bind")
    _validate_state_key(key)

    if not dry_run:
        cached = load_request("manage.scenarios.bind.set", request_id)
        if cached is not None:
            return cached

    service = ScenarioControlService(ctx)

    repo = ctx.scenarios_repo
    repo.ensure()
    exists = repo.get(scenario_id) is not None
    if not exists:
        result = {
            "status": "error",
            "code": "not_found",
            "scenario_id": scenario_id,
            "key": key,
            "value": value,
            "dry_run": dry_run,
            "request_id": request_id,
        }
        if not dry_run:
            _wrap_quota(lambda: save_request("manage.scenarios.bind.set", request_id, result))
        return result

    if not dry_run:
        _wrap_quota(lambda: service.set_binding(scenario_id, key, value))

    result = {
        "status": "ok",
        "scenario_id": scenario_id,
        "key": key,
        "value": value,
        "dry_run": dry_run,
        "request_id": request_id,
        "code": None,
    }
    if not dry_run:
        _wrap_quota(lambda: save_request("manage.scenarios.bind.set", request_id, result))
    return result


@tool(
    "resources.request",
    summary="Request access to a resource",
    stability="alpha",
    examples=["resources.request('microphone', scope='read', request_id='r1')"],
    input_schema={
        "type": "object",
        "properties": {
            "name": {"type": "string"},
            "scope": {"type": "string"},
            "request_id": {"type": "string"},
            "dry_run": {"type": "boolean", "default": False},
        },
        "required": ["name", "scope", "request_id"],
        "additionalProperties": False,
    },
    output_schema={
        "type": "object",
        "properties": {
            "status": {"type": "string"},
            "ticket_id": {"type": "string"},
            "dry_run": {"type": "boolean"},
            "details": {"type": "object"},
        },
        "required": ["status", "ticket_id", "dry_run", "details"],
        "additionalProperties": False,
    },
)
def resources_request(name: str, scope: str, request_id: str, dry_run: bool = False) -> dict:
    if not request_id:
        raise ValueError("request_id must be provided")

    ctx = require_cap("manage.resources.request")
    skill_id = _current_skill_id()
    payload = {"skill_id": skill_id, "name": name, "scope": scope, "status": "pending"}

    if not dry_run:
        cached = load_request("manage.resources.request", request_id)
        if cached is not None:
            return cached

    if dry_run:
        return {
            "status": "pending",
            "ticket_id": request_id,
            "dry_run": True,
            "details": payload,
        }

    service = ResourceRequestService(ctx)
    ticket = _wrap_quota(lambda: service.create_ticket(request_id, payload))
    result = {
        "status": ticket.status,
        "ticket_id": ticket.ticket_id,
        "dry_run": False,
        "details": ticket.payload,
    }
    _wrap_quota(lambda: save_request("manage.resources.request", request_id, result))
    return result


@tool(
    "resources.status",
    summary="Check ticket status",
    stability="alpha",
    examples=["resources.status('r1')"],
    input_schema={
        "type": "object",
        "properties": {
            "ticket_id": {"type": "string"},
        },
        "required": ["ticket_id"],
        "additionalProperties": False,
    },
    output_schema={
        "type": "object",
        "properties": {
            "status": {"type": "string"},
            "ticket": {"type": ["object", "null"]},
        },
        "required": ["status", "ticket"],
        "additionalProperties": False,
    },
)
def resources_status(ticket_id: str) -> dict:
    ctx = require_cap("manage.resources.status")
    service = ResourceRequestService(ctx)
    ticket = service.get_ticket(ticket_id)
    if ticket is None:
        return {"status": "not_found", "ticket": None}
    return {"status": ticket.status, "ticket": ticket.payload}
