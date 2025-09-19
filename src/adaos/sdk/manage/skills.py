"""Administrative skill management tools."""

from __future__ import annotations

from typing import Any, Mapping

from adaos.adapters.db.sqlite_skill_registry import SqliteSkillRegistry
from adaos.adapters.skills.git_repo import GitSkillRepository
from adaos.sdk.decorators import tool
from adaos.services.skill.manager import SkillManager

from .common import (
    SCHEMA_RESULT_ENVELOPE,
    _load_request,
    _meta_to_dict,
    _require_cap,
    _result,
    _safe_ns_key,
    _save_request,
    _wrap_quota,
)

__all__ = ["install", "uninstall", "list_installed"]


def _manager(ctx: Any) -> SkillManager:
    repo = GitSkillRepository(
        paths=ctx.paths,
        git=ctx.git,
        monorepo_url=ctx.settings.skills_monorepo_url,
        monorepo_branch=ctx.settings.skills_monorepo_branch,
    )
    registry = SqliteSkillRegistry(sql=ctx.sql)
    return SkillManager(
        git=ctx.git,
        paths=ctx.paths,
        caps=ctx.caps,
        settings=ctx.settings,
        registry=registry,
        repo=repo,
        bus=ctx.bus,
    )


_SKILL_ID_SCHEMA = {"type": "string", "minLength": 1}

_INSTALL_INPUT = {
    "type": "object",
    "properties": {
        "request_id": {"type": "string", "minLength": 1},
        "skill": _SKILL_ID_SCHEMA,
        "pin": {"type": "string"},
        "strict": {"type": "boolean", "default": True},
        "probe_tools": {"type": "boolean", "default": False},
        "dry_run": {"type": "boolean", "default": False},
    },
    "required": ["request_id", "skill"],
    "additionalProperties": True,
}

_UNINSTALL_INPUT = {
    "type": "object",
    "properties": {
        "request_id": {"type": "string", "minLength": 1},
        "skill": _SKILL_ID_SCHEMA,
        "dry_run": {"type": "boolean", "default": False},
    },
    "required": ["request_id", "skill"],
    "additionalProperties": True,
}

_LIST_OUTPUT = {
    "type": "object",
    "properties": {
        "skills": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "installed": {"type": "boolean"},
                    "active_version": {"type": ["string", "null"]},
                    "repo_url": {"type": ["string", "null"]},
                    "pin": {"type": ["string", "null"]},
                },
                "required": ["name", "installed"],
                "additionalProperties": True,
            },
        }
    },
    "required": ["skills"],
    "additionalProperties": False,
}


def _validation_to_dict(report: Any) -> Mapping[str, Any] | None:
    if report is None:
        return None
    issues = [
        {
            "level": issue.level,
            "code": issue.code,
            "message": issue.message,
            "where": getattr(issue, "where", None),
        }
        for issue in getattr(report, "issues", [])
    ]
    return {"ok": getattr(report, "ok", False), "issues": issues}


@tool(
    "skills.install",
    summary="install a skill from catalog or git",
    stability="stable",
    idempotent=True,
    examples=["skills.install(request_id='req-42', skill='weather_skill')"],
    input_schema=_INSTALL_INPUT,
    output_schema=SCHEMA_RESULT_ENVELOPE,
)
def install(
    request_id: str,
    skill: str,
    *,
    pin: str | None = None,
    strict: bool = True,
    probe_tools: bool = False,
    dry_run: bool = False,
) -> Mapping[str, Any]:
    ctx = _require_cap("skills.manage")
    namespace = _safe_ns_key("skills", "install")

    cached = _load_request(ctx, namespace, request_id)
    if cached is not None:
        return dict(cached)

    if dry_run:
        envelope = _result(
            ctx,
            request_id,
            "dry-run",
            dry_run=True,
            result={"skill": skill, "pin": pin},
        )
        return _save_request(ctx, namespace, request_id, envelope)

    mgr = _manager(ctx)
    install_fn = _wrap_quota(mgr.install)
    meta, report = install_fn(skill, pin=pin, validate=True, strict=strict, probe_tools=probe_tools)
    result_payload: dict[str, Any] = {"skill": _meta_to_dict(meta)}
    validation = _validation_to_dict(report)
    if validation is not None:
        result_payload["validation"] = validation
    envelope = _result(ctx, request_id, "ok", dry_run=False, result=result_payload)
    return _save_request(ctx, namespace, request_id, envelope)


@tool(
    "skills.uninstall",
    summary="remove an installed skill",
    stability="stable",
    idempotent=True,
    examples=["skills.uninstall(request_id='req-99', skill='weather_skill')"],
    input_schema=_UNINSTALL_INPUT,
    output_schema=SCHEMA_RESULT_ENVELOPE,
)
def uninstall(request_id: str, skill: str, *, dry_run: bool = False) -> Mapping[str, Any]:
    ctx = _require_cap("skills.manage")
    namespace = _safe_ns_key("skills", "uninstall")

    cached = _load_request(ctx, namespace, request_id)
    if cached is not None:
        return dict(cached)

    status = "dry-run"
    if not dry_run:
        mgr = _manager(ctx)
        uninstall_fn = _wrap_quota(mgr.uninstall)
        uninstall_fn(skill)
        status = "ok"
    envelope = _result(ctx, request_id, status, dry_run=dry_run, result={"skill": skill})
    return _save_request(ctx, namespace, request_id, envelope)


@tool(
    "skills.list",
    summary="list installed skills with registry metadata",
    stability="stable",
    examples=["skills.list()"],
    input_schema={"type": "object", "properties": {}, "additionalProperties": False},
    output_schema=_LIST_OUTPUT,
)
def list_installed() -> Mapping[str, Any]:
    ctx = _require_cap("skills.manage")
    mgr = _manager(ctx)
    records = mgr.list_installed()
    skills = [
        {
            "name": getattr(rec, "name", None),
            "installed": getattr(rec, "installed", True),
            "active_version": getattr(rec, "active_version", None),
            "repo_url": getattr(rec, "repo_url", None),
            "pin": getattr(rec, "pin", None),
        }
        for rec in records or []
    ]
    return {"skills": skills}
