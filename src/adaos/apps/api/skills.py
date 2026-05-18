from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from starlette.requests import ClientDisconnect

from adaos.adapters.db import SqliteSkillRegistry
from adaos.apps.api.auth import require_token
from adaos.services.agent_context import AgentContext, get_ctx
from adaos.services.skill.manager import SkillManager
from adaos.services.skill.artifacts import (
    request_upload_metadata,
    skill_upload_max_bytes,
    store_skill_upload,
)
from adaos.services.skill.update import SkillUpdateService
from adaos.services.eventbus import emit as bus_emit
from adaos.services.operations import submit_install_operation
from adaos.services.runtime_refresh import RuntimeRefreshError, rebuild_webspace_projection, refresh_skill_runtime
from adaos.services.skills_loader_importlib import ImportlibSkillsLoader
from adaos.services.workspace_registry import build_registry_entry, find_workspace_registry_entry, list_workspace_registry_entries
from adaos.services.yjs.webspace import default_webspace_id

import yaml
from packaging.version import Version, InvalidVersion


router = APIRouter(tags=["skills"], dependencies=[Depends(require_token)])
log = logging.getLogger(__name__)
_BACKGROUND_REBUILD_TASKS: set[asyncio.Task[Any]] = set()


def _get_manager(ctx: AgentContext = Depends(get_ctx)) -> SkillManager:
    repo = ctx.skills_repo
    registry = SqliteSkillRegistry(ctx.sql)
    return SkillManager(
        repo=repo,
        registry=registry,
        git=ctx.git,
        paths=ctx.paths,
        bus=getattr(ctx, "bus", None),
        caps=ctx.caps,
        settings=ctx.settings,
    )


def _to_mapping(obj: Any) -> Dict[str, Any]:
    try:
        return dict(obj)
    except Exception:
        pass
    try:
        return obj._asdict()  # type: ignore[attr-defined]
    except Exception:
        pass
    data: Dict[str, Any] = {}
    for key in ("name", "pin", "last_updated", "id", "path", "version", "active_version"):
        if hasattr(obj, key):
            value = getattr(obj, key)
            if key == "id" and hasattr(value, "value"):
                value = getattr(value, "value")
            data[key] = value
    return data or {"repr": repr(obj)}


def _schedule_webspace_rebuild(
    *,
    webspace_id: str,
    action: str,
    source_of_truth: str,
    reason: str,
) -> dict[str, Any]:
    try:
        from adaos.services.scenario.webspace_runtime import schedule_skill_runtime_rebuild

        return schedule_skill_runtime_rebuild(
            webspace_id=webspace_id,
            action=action,
            source_of_truth=source_of_truth,
            reason=reason,
        )
    except Exception:
        log.exception("failed to schedule coalesced webspace rebuild reason=%s webspace=%s", reason, webspace_id)

    async def _runner() -> None:
        try:
            await rebuild_webspace_projection(
                webspace_id=webspace_id,
                action=action,
                source_of_truth=source_of_truth,
            )
        except Exception:
            log.exception("background webspace rebuild failed reason=%s webspace=%s", reason, webspace_id)

    try:
        task = asyncio.create_task(_runner(), name=f"adaos-webspace-rebuild:{action}:{webspace_id}")
        _BACKGROUND_REBUILD_TASKS.add(task)
        task.add_done_callback(_BACKGROUND_REBUILD_TASKS.discard)
        return {
            "scheduled": True,
            "mode": "background",
            "webspace_id": webspace_id,
            "action": action,
            "source_of_truth": source_of_truth,
            "task": task.get_name(),
        }
    except Exception as exc:
        log.exception("failed to schedule webspace rebuild reason=%s webspace=%s", reason, webspace_id)
        return {
            "scheduled": False,
            "mode": "background",
            "webspace_id": webspace_id,
            "action": action,
            "source_of_truth": source_of_truth,
            "error": f"{type(exc).__name__}: {exc}",
        }


class UpdateReq(BaseModel):
    name: str
    dry_run: bool = False
    webspace_id: str | None = None
    defer_webspace_rebuild: bool = False
    force: bool | None = None


class UninstallReq(BaseModel):
    name: str
    webspace_id: str | None = None
    force: bool = False


class SyncReq(BaseModel):
    force: bool | None = None


def _safe_version(v: Any) -> Version | None:
    if v is None:
        return None
    raw = str(v).strip()
    if not raw:
        return None
    try:
        return Version(raw)
    except InvalidVersion:
        return None


def _read_registry_catalog_version(ctx: AgentContext, *, skill_id: str) -> str | None:
    entry = find_workspace_registry_entry(
        Path(ctx.paths.workspace_dir()),
        kind="skills",
        name_or_id=skill_id,
        fallback_to_scan=False,
    )
    if not isinstance(entry, dict):
        return None
    version = entry.get("version")
    if version is None:
        return None
    token = str(version).strip()
    return token or None


async def _reload_live_skill_handlers(ctx: AgentContext, skill_name: str) -> dict[str, Any]:
    try:
        return await ImportlibSkillsLoader().reload_skill_handlers(ctx.paths.skills_dir(), skill_name)
    except Exception as exc:
        log.debug("live skill handler reload failed skill=%s: %s", skill_name, exc, exc_info=True)
        return {"ok": False, "reason": "reload_failed", "error": str(exc)}


def _clean_version_text(value: object | None) -> str | None:
    token = str(value or "").strip()
    return token or None


def _repo_workspace_skills_root(ctx: AgentContext) -> Path | None:
    try:
        repo_root_attr = getattr(ctx.paths, "repo_root", None)
        repo_root = repo_root_attr() if callable(repo_root_attr) else repo_root_attr
        if not repo_root:
            return None
        candidate = Path(repo_root).expanduser().resolve() / ".adaos" / "workspace" / "skills"
        if candidate.exists():
            return candidate
    except Exception:
        return None
    return None


def _resolve_workspace_skill_source(ctx: AgentContext, skill_name: str, workspace_root: Path, workspace_skills_root: Path) -> Path:
    local_path = (workspace_skills_root / skill_name).resolve()
    if local_path.exists():
        return local_path
    repo_root = _repo_workspace_skills_root(ctx)
    if repo_root is not None:
        repo_path = (repo_root / skill_name).resolve()
        if repo_path.exists():
            return repo_path
    return local_path


def _read_local_artifact_version(kind: str, artifact_dir: Path) -> str | None:
    try:
        entry = build_registry_entry(kind, artifact_dir)
    except Exception:
        entry = None
    if not isinstance(entry, dict):
        return None
    return _clean_version_text(entry.get("version"))


def _resolve_list_skill_version(
    *,
    ctx: AgentContext,
    skill_name: str,
    row_version: object | None,
    registry_meta: dict[str, Any] | None,
) -> str:
    workspace_root = Path(ctx.paths.workspace_dir())
    workspace_skills_root = Path(ctx.paths.skills_workspace_dir())
    source_path = _resolve_workspace_skill_source(ctx, skill_name, workspace_root, workspace_skills_root)
    workspace_version = _read_local_artifact_version("skills", source_path)
    if not workspace_version and isinstance(registry_meta, dict):
        workspace_version = _clean_version_text(registry_meta.get("version"))
    return workspace_version or _clean_version_text(row_version) or "unknown"


class InstallReq(BaseModel):
    name: str
    pin: Optional[str] = None
    perform_validation: bool = False
    strict: bool = True
    probe_tools: bool = False
    async_operation: bool = False
    webspace_id: str | None = None


class PushReq(BaseModel):
    name: str
    message: str
    signoff: bool = False


# --- Runtime management API ---
class RuntimePrepareReq(BaseModel):
    name: str
    run_tests: bool = False
    slot: str | None = None


class RuntimeActivateReq(BaseModel):
    name: str
    slot: str | None = None
    version: str | None = None
    auto_prepare: bool = True
    webspace_id: str | None = "default"


class RuntimeNotifyActivatedReq(BaseModel):
    name: str
    space: str | None = "default"
    webspace_id: str | None = None
    defer_webspace_rebuild: bool = False


class RuntimeRebuildWebspaceReq(BaseModel):
    webspace_id: str | None = None


class RuntimeSetupReq(BaseModel):
    name: str


@router.get("/list")
async def list_skills(
    fs: bool = False,
    mgr: SkillManager = Depends(_get_manager),
    ctx: AgentContext = Depends(get_ctx),
):
    rows = mgr.list_installed()
    workspace_registry_by_name: dict[str, dict[str, Any]] = {}
    try:
        registry_items = list_workspace_registry_entries(Path(ctx.paths.workspace_dir()), kind="skills", fallback_to_scan=True)
    except Exception:
        registry_items = []
    for item in registry_items:
        if not isinstance(item, dict):
            continue
        item_name = str(item.get("name") or item.get("id") or "").strip()
        if item_name:
            workspace_registry_by_name[item_name] = item

    items = []
    for row in (rows or []):
        if not bool(getattr(row, "installed", True)):
            continue
        item = _to_mapping(row)
        name = str(item.get("name") or item.get("id") or item.get("repr") or "").strip()
        if name:
            item["version"] = _resolve_list_skill_version(
                ctx=ctx,
                skill_name=name,
                row_version=item.get("active_version") or item.get("version"),
                registry_meta=workspace_registry_by_name.get(name),
            )
        items.append(item)
    result: Dict[str, Any] = {"items": items}
    if fs:
        present = {m.id.value for m in mgr.list_present()}
        desired = {(i.get("name") or i.get("id") or i.get("repr")) for i in items}
        missing = sorted(desired - present)
        extra = sorted(present - desired)
        result["fs"] = {
            "present": sorted(present),
            "missing": missing,
            "extra": extra,
        }
    return result


@router.get("/installed-status")
async def installed_status(mgr: SkillManager = Depends(_get_manager), ctx: AgentContext = Depends(get_ctx)):
    """
    Installed skills with runtime slot and update hint (remote version > local version).
    """
    rows = mgr.list_installed()
    items: list[dict[str, Any]] = []

    for row in (rows or []):
        if not bool(getattr(row, "installed", True)):
            continue
        name = str(getattr(row, "name", "") or "").strip()
        if not name:
            continue

        meta = mgr.get(name)
        local_version = (getattr(meta, "version", None) if meta else None) or getattr(row, "active_version", None)
        local_version_s = str(local_version).strip() if local_version is not None else ""

        slot = ""
        try:
            st = mgr.runtime_status(name)
            slot = str(st.get("active_slot") or "").strip()
        except Exception:
            slot = ""

        remote_version_s = _read_registry_catalog_version(ctx, skill_id=name) or ""

        update_available = False
        lv = _safe_version(local_version_s)
        rv = _safe_version(remote_version_s)
        if lv is not None and rv is not None and rv > lv:
            update_available = True

        items.append(
            {
                "name": name,
                "version": local_version_s,
                "slot": slot,
                "remote_version": remote_version_s,
                "update_available": update_available,
            }
        )

    return {"ok": True, "items": items}


@router.post("/sync")
async def sync(body: SyncReq | None = None, mgr: SkillManager = Depends(_get_manager)):
    try:
        mgr.sync(force=(body.force if body is not None else None))
    except Exception as exc:
        # Surface the failure as a structured client error instead of a 500.
        # Common causes: dirty workspace, git remote/upstream misconfiguration, or merge conflicts.
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"ok": True}


@router.post("/install")
async def install(body: InstallReq, mgr: SkillManager = Depends(_get_manager)):
    webspace_id = body.webspace_id or default_webspace_id()
    if body.async_operation:
        operation = submit_install_operation(
            target_kind="skill",
            target_id=body.name,
            webspace_id=webspace_id,
        )
        return {
            "ok": True,
            "accepted": True,
            "operation_id": operation["operation_id"],
            "operation": operation,
        }
    # Best-effort sync to ensure monorepo workspace exists
    sync_error: Exception | None = None
    try:
        mgr.sync()
    except Exception as exc:
        # We may still be able to install if the skill is already materialized locally.
        # Keep the error to surface it if we later discover that the skill is missing.
        sync_error = exc
    try:
        result = mgr.install(
            body.name,
            pin=body.pin,
            validate=body.perform_validation,
            strict=body.strict,
            probe_tools=body.probe_tools,
        )
    except FileNotFoundError:
        # Retry once after an explicit sync in case the repo was missing.
        # If the best-effort sync already failed, surface that as a client error.
        if sync_error is not None:
            raise HTTPException(status_code=409, detail=str(sync_error)) from sync_error
        try:
            mgr.sync()
        except Exception as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        result = mgr.install(
            body.name,
            pin=body.pin,
            validate=body.perform_validation,
            strict=body.strict,
            probe_tools=body.probe_tools,
        )
    if isinstance(result, tuple):
        meta, report = result
    else:
        meta, report = result, None
    payload: Dict[str, Any] = {
        "ok": True,
        "skill": {
            "id": getattr(meta, "id", None).value if getattr(meta, "id", None) else body.name,
            "version": getattr(meta, "version", None),
            "path": str(getattr(meta, "path", "")),
        },
    }
    prep = mgr.prepare_runtime(body.name, run_tests=False)
    slot = mgr.activate_for_space(
        body.name,
        version=getattr(prep, "version", None),
        slot=getattr(prep, "slot", None),
        space="default",
        webspace_id=webspace_id,
    )
    payload["runtime"] = {
        "version": getattr(prep, "version", None),
        "slot": slot,
        "prepared": getattr(prep, "slot", None),
        "webspace_id": webspace_id,
    }
    try:
        await rebuild_webspace_projection(
            webspace_id=webspace_id,
            action="skill_install_sync",
            source_of_truth="skill_runtime",
        )
    except Exception:
        log.exception("webspace rebuild failed after skill install: %s", body.name)
    if report is not None:
        if hasattr(report, "to_dict"):
            payload["report"] = report.to_dict()  # type: ignore[call-arg]
        else:
            payload["report"] = repr(report)
    return payload


@router.post("/uninstall")
async def uninstall(body: UninstallReq, mgr: SkillManager = Depends(_get_manager)):
    webspace_id = body.webspace_id or default_webspace_id()
    mgr.uninstall(
        body.name,
        force=bool(body.force),
    )
    try:
        await rebuild_webspace_projection(
            webspace_id=webspace_id,
            action="skill_uninstall_sync",
            source_of_truth="skill_runtime",
        )
    except Exception:
        log.exception("webspace rebuild failed after skill uninstall: %s", body.name)
    return {"ok": True}


@router.get("/{name}")
async def get_skill(name: str, mgr: SkillManager = Depends(_get_manager)):
    meta = mgr.get(name)
    if not meta:
        return {"ok": False, "reason": "not-found"}
    return {"ok": True, "skill": _to_mapping(meta)}


@router.delete("/{name}")
async def remove(name: str, mgr: SkillManager = Depends(_get_manager)):
    mgr.uninstall(name)
    try:
        await rebuild_webspace_projection(
            webspace_id=default_webspace_id(),
            action="skill_uninstall_sync",
            source_of_truth="skill_runtime",
        )
    except Exception:
        log.exception("webspace rebuild failed after skill delete: %s", name)
    return {"ok": True}


@router.put("/{name}/files/{filename:path}")
async def upload_skill_file(
    name: str,
    filename: str,
    request: Request,
    purpose: str | None = None,
    ctx: AgentContext = Depends(get_ctx),
):
    metadata = request_upload_metadata(request.headers)
    content_length = metadata.get("content_length")
    max_bytes = skill_upload_max_bytes()
    if isinstance(content_length, int) and max_bytes > 0 and content_length > max_bytes:
        raise HTTPException(status_code=413, detail=f"upload exceeds max size: {max_bytes} bytes")
    try:
        return await store_skill_upload(
            skills_root=Path(ctx.paths.skills_workspace_dir()),
            skill_name=name,
            filename=filename,
            chunks=request.stream(),
            purpose=purpose,
            content_type=metadata.get("content_type"),
            max_bytes=max_bytes,
        )
    except ClientDisconnect as exc:
        raise HTTPException(status_code=499, detail="upload client disconnected") from exc
    except ValueError as exc:
        raise HTTPException(status_code=413 if "max size" in str(exc) else 400, detail=str(exc)) from exc


@router.post("/push")
async def push(body: PushReq, mgr: SkillManager = Depends(_get_manager)):
    revision = mgr.push(body.name, body.message, signoff=body.signoff)
    return {"ok": True, "revision": revision}


# --- Runtime management endpoints ---


@router.post("/runtime/prepare")
async def runtime_prepare(body: RuntimePrepareReq, mgr: SkillManager = Depends(_get_manager)):
    result = mgr.prepare_runtime(body.name, run_tests=body.run_tests, preferred_slot=body.slot)
    payload = {
        "ok": True,
        "name": result.name,
        "version": result.version,
        "slot": result.slot,
        "resolved_manifest": str(result.resolved_manifest),
        "tests": {k: v.status for k, v in (result.tests or {}).items()},
    }
    return payload


@router.post("/runtime/activate")
async def runtime_activate(
    body: RuntimeActivateReq,
    mgr: SkillManager = Depends(_get_manager),
    ctx: AgentContext = Depends(get_ctx),
):
    webspace_id = body.webspace_id or "default"
    try:
        slot = mgr.activate_for_space(body.name, version=body.version, slot=body.slot, space="default", webspace_id=webspace_id)
        reload_result = await _reload_live_skill_handlers(ctx, body.name)
        return {"ok": True, "slot": slot, "handler_reload": reload_result}
    except RuntimeError as exc:
        msg = str(exc).lower()
        if not body.auto_prepare or ("is not prepared" not in msg and "no installed versions" not in msg):
            # expose as 422 Unprocessable if activation cannot proceed
            from fastapi import HTTPException

            raise HTTPException(status_code=422, detail=str(exc))
        # auto-prepare then retry
        pref_slot = body.slot
        prep = mgr.prepare_runtime(body.name, run_tests=False, preferred_slot=pref_slot)
        slot = mgr.activate_for_space(body.name, version=prep.version, slot=prep.slot, space="default", webspace_id=webspace_id)
        reload_result = await _reload_live_skill_handlers(ctx, body.name)
        return {"ok": True, "slot": slot, "prepared": prep.slot, "handler_reload": reload_result}


@router.post("/runtime/notify-activated")
async def runtime_notify_activated(body: RuntimeNotifyActivatedReq):
    """
    Lightweight hook to broadcast a skills.activated event on the hub bus
    without touching runtime slots (used by CLI after local activation).
    """
    ctx = get_ctx()
    bus = getattr(ctx, "bus", None)
    if bus is None:
        return {"ok": False, "reason": "bus-unavailable"}
    space = (body.space or "default").strip() or "default"
    webspace_id = body.webspace_id or default_webspace_id()
    payload: Dict[str, Any] = {
        "skill_name": body.name,
        "space": space,
        "webspace_id": webspace_id,
        "defer_webspace_rebuild": bool(body.defer_webspace_rebuild),
    }
    reload_result = await _reload_live_skill_handlers(ctx, body.name)
    bus_emit(bus, "skills.activated", payload, "api.skills")
    return {"ok": True, "handler_reload": reload_result}


@router.post("/runtime/rebuild-webspace")
async def runtime_rebuild_webspace(body: RuntimeRebuildWebspaceReq):
    webspace_id = body.webspace_id or default_webspace_id()
    await rebuild_webspace_projection(
        webspace_id=webspace_id,
        action="skill_batch_runtime_sync",
        source_of_truth="skill_runtime",
    )
    return {"ok": True, "accepted": True, "webspace_id": webspace_id}


@router.get("/runtime/status/{name}")
async def runtime_status(name: str, mgr: SkillManager = Depends(_get_manager)):
    state = mgr.runtime_status(name)
    return {"ok": True, "state": state}


@router.post("/runtime/setup")
async def runtime_setup(body: RuntimeSetupReq, mgr: SkillManager = Depends(_get_manager)):
    try:
        result = mgr.setup_skill(body.name)
        if isinstance(result, dict):
            return {"ok": bool(result.get("ok", True)), **result}
        return {"ok": True, "result": result}
    except RuntimeError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except Exception as exc:
        log.exception("runtime setup failed: %s", body.name)
        raise HTTPException(status_code=500, detail=str(exc) or "runtime setup failed") from exc


@router.post("/update")
async def update_skill(body: UpdateReq, ctx: AgentContext = Depends(get_ctx)):
    service = SkillUpdateService(ctx)
    try:
        kwargs: dict[str, Any] = {"dry_run": body.dry_run}
        if body.force is not None:
            kwargs["force"] = body.force
        result = await asyncio.to_thread(service.request_update, body.name, **kwargs)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except Exception as exc:
        log.exception("skill update failed: %s", body.name)
        raise HTTPException(status_code=500, detail=str(exc) or "skill update failed") from exc
    webspace_id = body.webspace_id or default_webspace_id()
    runtime_refresh: dict[str, Any] = {}
    webspace_rebuild: dict[str, Any] = {
        "scheduled": False,
        "mode": "deferred" if bool(body.defer_webspace_rebuild) else "not_requested",
        "webspace_id": webspace_id,
    }
    if not body.dry_run:
        mgr = _get_manager(ctx)
        source_version = str(result.version or "").strip()
        try:
            runtime_refresh = await asyncio.to_thread(
                refresh_skill_runtime,
                mgr,
                body.name,
                webspace_id=webspace_id,
                source_version=source_version,
                migrate_runtime=True,
                ensure_installed=False,
                require_active_version=True,
            )
        except RuntimeRefreshError as exc:
            log.exception("runtime refresh failed after skill update: %s", body.name)
            raise HTTPException(
                status_code=409,
                detail={
                    "message": f"runtime refresh failed after skill update: {exc}",
                    "runtime_refresh": exc.payload,
                },
            ) from exc
        except Exception as exc:
            log.exception("runtime refresh failed after skill update: %s", body.name)
            raise HTTPException(status_code=409, detail=f"runtime refresh failed after skill update: {exc}") from exc
        bus = getattr(ctx, "bus", None)
        if bus is not None:
            bus_emit(
                bus,
                "skills.updated",
                {
                    "name": body.name,
                    "webspace_id": webspace_id,
                    "defer_webspace_rebuild": bool(body.defer_webspace_rebuild),
                },
                "api.skills",
            )
            bus_emit(
                bus,
                "skills.activated",
                {
                    "skill_name": body.name,
                    "space": "default",
                    "webspace_id": webspace_id,
                    "defer_webspace_rebuild": bool(body.defer_webspace_rebuild),
                },
                "api.skills",
            )
        if not body.defer_webspace_rebuild:
            webspace_rebuild = _schedule_webspace_rebuild(
                webspace_id=webspace_id,
                action="skill_update_sync",
                source_of_truth="skill_runtime",
                reason=f"skill_update:{body.name}",
            )
    return {
        "ok": True,
        "updated": result.updated,
        "version": result.version,
        "runtime_refresh": runtime_refresh,
        "webspace_rebuild": webspace_rebuild,
    }
