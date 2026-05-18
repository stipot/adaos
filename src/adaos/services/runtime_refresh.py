from __future__ import annotations

import asyncio
from typing import Any


class RuntimeRefreshError(RuntimeError):
    """Runtime refresh failed after producing a diagnostic operation payload."""

    def __init__(self, message: str, payload: dict[str, Any]) -> None:
        super().__init__(message)
        self.payload = dict(payload)


def _default_webspace_id() -> str:
    from adaos.services.yjs.webspace import default_webspace_id

    return default_webspace_id()


async def rebuild_webspace_projection(
    *,
    webspace_id: str | None = None,
    action: str,
    source_of_truth: str,
) -> dict[str, Any]:
    from adaos.services.scenario.webspace_runtime import rebuild_webspace_from_sources

    target_webspace = str(webspace_id or "").strip() or _default_webspace_id()
    await rebuild_webspace_from_sources(
        target_webspace,
        action=str(action or "").strip() or "runtime_refresh",
        source_of_truth=str(source_of_truth or "").strip() or "skill_runtime",
    )
    return {
        "ok": True,
        "accepted": True,
        "webspace_id": target_webspace,
        "action": str(action or "").strip() or "runtime_refresh",
        "source_of_truth": str(source_of_truth or "").strip() or "skill_runtime",
    }


def rebuild_webspace_projection_sync(
    *,
    webspace_id: str | None = None,
    action: str,
    source_of_truth: str,
) -> dict[str, Any]:
    return asyncio.run(
        rebuild_webspace_projection(
            webspace_id=webspace_id,
            action=action,
            source_of_truth=source_of_truth,
        )
    )


def _record_stage(payload: dict[str, Any], stage: str, *, ok: bool, **fields: Any) -> None:
    entry: dict[str, Any] = {"stage": stage, "ok": bool(ok)}
    for key, value in fields.items():
        if value is None:
            continue
        if value == "" or value == {} or value == []:
            continue
        entry[key] = value
    payload.setdefault("lifecycle_stages", []).append(entry)


def refresh_skill_runtime(
    mgr: Any,
    skill_name: str,
    *,
    webspace_id: str | None = None,
    source_version: str | None = None,
    migrate_runtime: bool = True,
    ensure_installed: bool = False,
    require_active_version: bool = False,
) -> dict[str, Any]:
    target_webspace = str(webspace_id or "").strip() or _default_webspace_id()
    expected_version = str(source_version or "").strip()
    payload: dict[str, Any] = {
        "skill": str(skill_name or "").strip(),
        "webspace_id": target_webspace,
        "source_version": expected_version,
        "ok": False,
        "runtime_updated": False,
        "runtime_migrated": False,
        "active_converged": False,
        "prepared_version": "",
        "prepared_slot": "",
        "activated_slot": "",
        "failed_stage": "",
        "failure_reason": "",
        "lifecycle_stages": [],
    }
    runtime_status_before: dict[str, Any] = {}
    try:
        runtime_status_before = mgr.runtime_status(skill_name)
    except Exception:
        runtime_status_before = {}
    runtime_version_before = str(runtime_status_before.get("version") or "").strip()
    payload["active_version_before"] = runtime_version_before
    payload["active_slot_before"] = str(runtime_status_before.get("active_slot") or "").strip()
    try:
        runtime_result = mgr.runtime_update(skill_name, space="workspace")
        payload["runtime_updated"] = True
        payload["runtime_update_result"] = runtime_result
        _record_stage(payload, "runtime_update", ok=True)
    except Exception as exc:
        payload["runtime_update_error"] = str(exc)
        _record_stage(payload, "runtime_update", ok=False, error=str(exc), fatal=False)
        runtime_result = {}
    should_prepare = False
    if source_version is not None:
        should_prepare = bool(str(source_version or "").strip() and str(source_version or "").strip() != runtime_version_before)
    if isinstance(runtime_result, dict) and not bool(runtime_result.get("ok", True)):
        should_prepare = True
    if migrate_runtime and should_prepare:
        if ensure_installed:
            mgr.install(skill_name, validate=False)
        try:
            runtime = mgr.prepare_runtime(skill_name, run_tests=False)
        except Exception as exc:
            message = f"runtime prepare failed after skill update: {exc}"
            payload["failed_stage"] = "prepare"
            payload["failure_reason"] = str(exc)
            payload["error"] = message
            _record_stage(payload, "prepare", ok=False, error=str(exc))
            try:
                runtime_status_after = mgr.runtime_status(skill_name)
            except Exception:
                runtime_status_after = {}
            payload["active_version_after"] = str(runtime_status_after.get("version") or "").strip()
            payload["active_slot_after"] = str(runtime_status_after.get("active_slot") or "").strip()
            raise RuntimeRefreshError(message, payload) from exc
        version = getattr(runtime, "version", None)
        slot = getattr(runtime, "slot", None)
        payload["prepared_version"] = str(version or "").strip()
        payload["prepared_slot"] = str(slot or "").strip()
        payload["data_migration"] = dict(getattr(runtime, "data_migration", None) or {})
        _record_stage(payload, "prepare", ok=True, version=version, slot=slot)
        try:
            active_slot = mgr.activate_for_space(
                skill_name,
                version=version,
                slot=slot,
                space="default",
                webspace_id=target_webspace,
            )
        except Exception as exc:
            message = f"runtime activation failed after skill update: {exc}"
            payload["failed_stage"] = "activate"
            payload["failure_reason"] = str(exc)
            payload["error"] = message
            _record_stage(payload, "activate", ok=False, version=version, slot=slot, error=str(exc))
            try:
                runtime_status_after = mgr.runtime_status(skill_name)
            except Exception:
                runtime_status_after = {}
            payload["active_version_after"] = str(runtime_status_after.get("version") or "").strip()
            payload["active_slot_after"] = str(runtime_status_after.get("active_slot") or "").strip()
            raise RuntimeRefreshError(message, payload) from exc
        payload["runtime_migrated"] = True
        payload["migrated_version"] = version
        payload["migrated_slot"] = active_slot
        payload["activated_slot"] = str(active_slot or "").strip()
        _record_stage(payload, "activate", ok=True, version=version, slot=active_slot)
    else:
        _record_stage(payload, "prepare", ok=True, skipped=True)
        _record_stage(payload, "activate", ok=True, skipped=True)
    runtime_status_after: dict[str, Any] = {}
    try:
        runtime_status_after = mgr.runtime_status(skill_name)
    except Exception:
        runtime_status_after = {}
    runtime_version_after = str(runtime_status_after.get("version") or "").strip()
    payload["active_version_after"] = runtime_version_after
    payload["active_slot_after"] = str(runtime_status_after.get("active_slot") or "").strip()
    if expected_version:
        payload["active_converged"] = runtime_version_after == expected_version
    else:
        payload["active_converged"] = bool(runtime_version_after)
    _record_stage(
        payload,
        "converge",
        ok=bool(payload["active_converged"]),
        expected_version=expected_version,
        active_version=runtime_version_after,
    )
    if require_active_version and expected_version and runtime_version_after != expected_version:
        message = (
            "runtime active version did not converge after skill update: "
            f"skill={skill_name} expected={expected_version} active={runtime_version_after or 'none'}"
        )
        payload["failed_stage"] = "converge"
        payload["failure_reason"] = message
        payload["error"] = message
        raise RuntimeRefreshError(message, payload)
    payload["ok"] = True
    return payload
