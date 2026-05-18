from __future__ import annotations

from typing import Any

from adaos.adapters.db import SqliteScenarioRegistry, SqliteSkillRegistry
from adaos.services.bootstrap import load_config
from adaos.services.capacity import get_local_capacity
from adaos.services.agent_context import AgentContext, get_ctx
from adaos.services.device_inventory import list_devices as list_device_inventory_records
from adaos.services.scenario.manager import ScenarioManager
from adaos.services.skill.manager import SkillManager
from adaos.services.system_model.governance import apply_governance_defaults
from adaos.services.system_model.mappers import (
    canonical_object_from_browser_session,
    canonical_object_from_capacity_snapshot,
    canonical_object_from_device_endpoint,
    canonical_object_from_io_capacity_entry,
    canonical_object_from_scenario_item,
    canonical_object_from_skill_status,
    canonical_object_from_user_profile,
    canonical_object_from_workspace_manifest,
)
from adaos.services.system_model.model import CanonicalKind, canonical_ref
from adaos.services.user.profile import UserProfileService
from adaos.services.workspaces import index as workspace_index


def _ctx(ctx: AgentContext | None = None) -> AgentContext:
    return ctx or get_ctx()


def _governance_refs() -> tuple[str | None, str | None]:
    conf = load_config()
    subnet_value = str(getattr(conf, "subnet_id", "") or "").strip()
    owner_value = str(getattr(conf, "owner_id", "") or "").strip()
    tenant_id = f"subnet:{subnet_value}" if subnet_value else None
    owner_id = canonical_ref(CanonicalKind.PROFILE, owner_value) or (f"profile:{owner_value}" if owner_value else None)
    return tenant_id, owner_id


def _governed(obj: Any):
    tenant_id, owner_id = _governance_refs()
    return apply_governance_defaults(obj, tenant_id=tenant_id, owner_id=owner_id)


def _skill_manager(ctx: AgentContext | None = None) -> SkillManager:
    runtime = _ctx(ctx)
    return SkillManager(
        repo=runtime.skills_repo,
        registry=SqliteSkillRegistry(runtime.sql),
        git=runtime.git,
        paths=runtime.paths,
        bus=getattr(runtime, "bus", None),
        caps=runtime.caps,
        settings=runtime.settings,
    )


def _scenario_manager(ctx: AgentContext | None = None) -> ScenarioManager:
    runtime = _ctx(ctx)
    return ScenarioManager(
        repo=runtime.scenarios_repo,
        registry=SqliteScenarioRegistry(runtime.sql),
        git=runtime.git,
        paths=runtime.paths,
        bus=getattr(runtime, "bus", None),
        caps=runtime.caps,
    )


def skill_object(name: str, *, ctx: AgentContext | None = None):
    mgr = _skill_manager(ctx)
    meta = mgr.get(name)
    slot = ""
    try:
        state = mgr.runtime_status(name)
        if isinstance(state, dict):
            slot = str(state.get("active_slot") or "").strip()
    except Exception:
        slot = ""
    version = str(getattr(meta, "version", None) or "").strip() if meta is not None else ""
    payload: dict[str, Any] = {
        "name": name,
        "version": version or None,
        "slot": slot or None,
        "update_available": False,
    }
    return _governed(canonical_object_from_skill_status(payload))


def installed_skill_objects(*, ctx: AgentContext | None = None) -> list[Any]:
    mgr = _skill_manager(ctx)
    objects: list[Any] = []
    for row in list(mgr.list_installed() or []):
        if not bool(getattr(row, "installed", True)):
            continue
        name = str(getattr(row, "name", "") or "").strip()
        if not name:
            continue
        objects.append(skill_object(name, ctx=ctx))
    return objects


def scenario_object(name: str, *, ctx: AgentContext | None = None):
    mgr = _scenario_manager(ctx)
    rows = list(mgr.list_installed() or [])
    for row in rows:
        row_name = str(getattr(row, "name", "") or "").strip()
        if row_name == name:
            return _governed(canonical_object_from_scenario_item(
                {
                    "name": name,
                    "version": getattr(row, "version", None),
                    "path": getattr(row, "path", None),
                }
            ))
    return _governed(canonical_object_from_scenario_item({"name": name}))


def installed_scenario_objects(*, ctx: AgentContext | None = None) -> list[Any]:
    mgr = _scenario_manager(ctx)
    objects: list[Any] = []
    for row in list(mgr.list_installed() or []):
        name = str(getattr(row, "name", "") or "").strip()
        if not name:
            continue
        objects.append(
            _governed(canonical_object_from_scenario_item(
                {
                    "name": name,
                    "version": getattr(row, "version", None),
                    "path": getattr(row, "path", None),
                }
            ))
        )
    return objects


def current_profile_object(*, ctx: AgentContext | None = None):
    svc = UserProfileService(_ctx(ctx))
    return _governed(canonical_object_from_user_profile(svc.get_profile()))


def workspace_object(workspace_id: str):
    row = workspace_index.get_workspace(workspace_id) or workspace_index.ensure_workspace(workspace_id)
    return _governed(canonical_object_from_workspace_manifest(row))


def workspace_objects() -> list[Any]:
    return [_governed(canonical_object_from_workspace_manifest(row)) for row in list(workspace_index.list_workspaces() or [])]


def _browser_session_payloads() -> list[dict[str, Any]]:
    merged: dict[str, dict[str, Any]] = {}
    try:
        from adaos.services.yjs.gateway_ws import active_browser_session_snapshot

        snapshot = active_browser_session_snapshot()
    except Exception:
        snapshot = {}
    for item in list(snapshot.get("peers") or []):
        if not isinstance(item, dict):
            continue
        device_id = str(item.get("device_id") or item.get("id") or "").strip()
        if not device_id:
            continue
        merged[device_id] = dict(item)
    try:
        from adaos.services.webrtc.peer import webrtc_peer_snapshot

        snapshot = webrtc_peer_snapshot()
    except Exception:
        snapshot = {}
    for item in list(snapshot.get("peers") or []):
        if not isinstance(item, dict):
            continue
        device_id = str(item.get("device_id") or item.get("id") or "").strip()
        if not device_id:
            continue
        merged[device_id] = {**merged.get(device_id, {}), **item}
    try:
        from adaos.services.access_links import browser_snapshot

        link_entries = list(browser_snapshot() or [])
    except Exception:
        link_entries = []
    for item in link_entries:
        if not isinstance(item, dict):
            continue
        device_id = str(item.get("device_id") or item.get("id") or "").strip()
        if not device_id or device_id not in merged:
            continue
        merged[device_id] = {**item, **merged[device_id]}
    return [merged[key] for key in sorted(merged)]


def browser_session_objects() -> list[Any]:
    return [_governed(canonical_object_from_browser_session(item)) for item in _browser_session_payloads()]


def device_objects() -> list[Any]:
    records: dict[str, dict[str, Any]] = {}

    for item in list_device_inventory_records():
        if not isinstance(item, dict):
            continue
        identity = item.get("identity") if isinstance(item.get("identity"), dict) else {}
        observation = item.get("observation") if isinstance(item.get("observation"), dict) else {}
        device_kind = str(item.get("kind") or "device").strip().lower() or "device"
        browser_device_id = str(identity.get("browser_device_id") or identity.get("link_id") or "").strip()
        member_node_id = str(identity.get("node_id") or identity.get("link_id") or "").strip()
        device_key = browser_device_id if device_kind == "browser" and browser_device_id else str(item.get("ref") or member_node_id or "unknown").strip() or "unknown"
        workspace_ids: list[str] = []
        last_webspace_id = str(observation.get("last_webspace_id") or "").strip()
        if last_webspace_id:
            workspace_ids.append(last_webspace_id)
        session_ids: list[str] = []
        if device_kind == "browser" and browser_device_id:
            session_ids.append(f"browser:{browser_device_id}")
        elif device_kind == "member" and member_node_id:
            session_ids.append(f"member:{member_node_id}")
        records[device_key] = {
            **item,
            "device_id": device_key,
            "device_kind": device_kind,
            "workspace_ids": workspace_ids,
            "session_ids": session_ids,
            "online": observation.get("online"),
            "last_seen": observation.get("last_seen_at"),
            "source": "device_inventory",
        }

    for row in list(workspace_index.list_workspaces() or []):
        device_id = str(getattr(row, "device_binding", "") or "").strip()
        if not device_id:
            continue
        record = records.setdefault(
            device_id,
            {
                "device_id": device_id,
                "device_kind": "workspace_binding",
                "workspace_ids": [],
                "session_ids": [],
                "online": None,
                "source": "workspace_manifest",
            },
        )
        workspace_id = str(getattr(row, "workspace_id", "") or "").strip()
        workspace_ids = record.setdefault("workspace_ids", [])
        if workspace_id and workspace_id not in workspace_ids:
            workspace_ids.append(workspace_id)

    return [
        _governed(canonical_object_from_device_endpoint(item))
        for item in sorted(records.values(), key=lambda entry: str(entry.get("device_id") or ""))
    ]


def local_capacity_object(*, node_id: str | None = None):
    return _governed(canonical_object_from_capacity_snapshot(get_local_capacity(), node_id=node_id))


def local_io_objects(*, node_id: str | None = None) -> list[Any]:
    snapshot = get_local_capacity()
    io_items = snapshot.get("io") if isinstance(snapshot.get("io"), list) else []
    return [
        _governed(canonical_object_from_io_capacity_entry(item, node_id=node_id))
        for item in io_items
        if isinstance(item, dict)
    ]


__all__ = [
    "browser_session_objects",
    "current_profile_object",
    "device_objects",
    "installed_scenario_objects",
    "installed_skill_objects",
    "local_capacity_object",
    "local_io_objects",
    "scenario_object",
    "skill_object",
    "workspace_object",
    "workspace_objects",
]
