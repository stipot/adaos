from __future__ import annotations
from typing import Any, Dict, Optional
from fastapi import APIRouter, Depends
from pydantic import BaseModel

from adaos.apps.api.auth import require_token
from adaos.services.agent_context import get_ctx, AgentContext
from adaos.services.node_config import load_config
from adaos.services.node_display import node_display_from_config, node_display_from_directory_node
from adaos.services.registry.subnet_directory import get_directory
from adaos.services.scenario.manager import ScenarioManager
from adaos.services.scenario.webspace_runtime import rebuild_webspace_from_sources
from adaos.adapters.db import SqliteScenarioRegistry
from adaos.services.operations import submit_install_operation, submit_update_operation
from adaos.services.yjs.webspace import default_webspace_id


router = APIRouter(tags=["scenarios"], dependencies=[Depends(require_token)])


# --- DI: получаем менеджер так же, как в CLI ---------------------------------
def _get_manager(ctx: AgentContext = Depends(get_ctx)) -> ScenarioManager:
    repo = ctx.scenarios_repo
    reg = SqliteScenarioRegistry(ctx.sql)
    return ScenarioManager(repo=repo, registry=reg, git=ctx.git, paths=ctx.paths, bus=ctx.bus, caps=ctx.caps)


# --- helpers -----------------------------------------------------------------
def _to_mapping(obj: Any) -> Dict[str, Any]:
    # sqlite3.Row, NamedTuple, dataclass, simple objects — мягкая нормализация
    try:
        return dict(obj)
    except Exception:
        pass
    try:
        return obj._asdict()  # type: ignore[attr-defined]
    except Exception:
        pass
    d: Dict[str, Any] = {}
    for k in ("name", "pin", "last_updated", "id", "path", "version"):
        if hasattr(obj, k):
            v = getattr(obj, k)
            # id может быть сложным типом
            if k == "id":
                if hasattr(v, "value"):
                    v = getattr(v, "value")
                else:
                    v = str(v)
            d[k] = v
    return d or {"repr": repr(obj)}


def _meta_id(meta: Any) -> str:
    mid = getattr(meta, "id", None)
    if mid is None:
        return str(meta)
    return getattr(mid, "value", str(mid))


def _local_node_id() -> str:
    try:
        conf = load_config()
        node_id = str(getattr(conf, "node_id", "") or "").strip()
        if node_id:
            return node_id
    except Exception:
        pass
    return "hub"


def _local_node_label() -> str:
    try:
        conf = load_config()
        return str(node_display_from_config(conf).get("node_label") or "").strip() or _local_node_id()
    except Exception:
        return _local_node_id()


def _node_label_from_directory(node: Dict[str, Any]) -> str:
    return str(node_display_from_directory_node(node).get("node_label") or "").strip() or str(node.get("node_id") or "").strip() or "hub"


# --- API (тонкий фасад CLI) --------------------------------------------------
class InstallReq(BaseModel):
    name: str
    pin: Optional[str] = None
    async_operation: bool = False
    webspace_id: str | None = None


class UpdateReq(BaseModel):
    name: str
    async_operation: bool = False
    webspace_id: str | None = None


class PushReq(BaseModel):
    name: str
    message: str
    signoff: bool = False

class UninstallReq(BaseModel):
    name: str
    webspace_id: str | None = None


@router.get("/list")
async def list_scenarios(fs: bool = False, mgr: ScenarioManager = Depends(_get_manager)):
    rows = mgr.list_installed()
    items: list[Dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    local_node_id = _local_node_id()
    local_node_display = node_display_from_config(load_config())
    for row in rows or []:
        item = _to_mapping(row)
        scenario_id = str(item.get("name") or item.get("id") or item.get("repr") or "").strip()
        if not scenario_id:
            continue
        key = (local_node_id, scenario_id)
        if key in seen:
            continue
        seen.add(key)
        item["id"] = scenario_id
        item["name"] = scenario_id
        item["node_id"] = local_node_id
        item["node_label"] = str(local_node_display.get("node_label") or _local_node_label())
        item["node_compact_label"] = local_node_display.get("node_compact_label")
        item["node_index"] = local_node_display.get("node_index")
        item["node_color"] = local_node_display.get("node_color")
        item["source"] = "local_installed"
        items.append(item)
    try:
        conf = load_config()
        if str(getattr(conf, "role", "") or "").strip().lower() == "hub":
            for node in get_directory().list_known_nodes():
                node_id = str(node.get("node_id") or "").strip()
                if not node_id:
                    continue
                node_display = node_display_from_directory_node(node)
                node_label = str(node_display.get("node_label") or _node_label_from_directory(node))
                capacity = node.get("capacity") if isinstance(node.get("capacity"), dict) else {}
                scenarios = capacity.get("scenarios") if isinstance(capacity.get("scenarios"), list) else []
                for scenario in scenarios:
                    if not isinstance(scenario, dict):
                        continue
                    scenario_id = str(scenario.get("name") or scenario.get("id") or "").strip()
                    if not scenario_id:
                        continue
                    key = (node_id, scenario_id)
                    if key in seen:
                        continue
                    seen.add(key)
                    items.append({
                        **scenario,
                        "id": scenario_id,
                        "name": scenario_id,
                        "node_id": node_id,
                        "node_label": node_label,
                        "node_compact_label": node_display.get("node_compact_label"),
                        "node_index": node_display.get("node_index"),
                        "node_color": node_display.get("node_color"),
                        "source": "subnet_capacity",
                    })
    except Exception:
        pass
    result: Dict[str, Any] = {"items": items}
    if fs:
        present = {_meta_id(m) for m in mgr.list_present()}
        desired = {(i.get("name") or i.get("id") or i.get("repr")) for i in items}
        missing = sorted(desired - present)
        extra = sorted(present - desired)
        result["fs"] = {
            "present": sorted(present),
            "missing": missing,
            "extra": extra,
        }
    return result


@router.post("/sync")
async def sync(mgr: ScenarioManager = Depends(_get_manager)):
    mgr.sync()
    return {"ok": True}


@router.post("/install")
async def install(body: InstallReq, mgr: ScenarioManager = Depends(_get_manager)):
    if body.async_operation:
        operation = submit_install_operation(
            target_kind="scenario",
            target_id=body.name,
            webspace_id=body.webspace_id,
        )
        return {
            "ok": True,
            "accepted": True,
            "operation_id": operation["operation_id"],
            "operation": operation,
        }
    webspace_id = body.webspace_id or default_webspace_id()
    meta = mgr.install_with_deps(body.name, pin=body.pin, webspace_id=webspace_id)
    try:
        await rebuild_webspace_from_sources(
            webspace_id,
            action="scenario_install_sync",
            scenario_id=body.name,
            source_of_truth="scenario_projection",
        )
    except Exception:
        pass
    # приведём к компактному виду как в CLI-эхо
    return {
        "ok": True,
        "scenario": {
            "id": _meta_id(meta),
            "version": getattr(meta, "version", None),
            "path": str(getattr(meta, "path", "")),
        },
        "dependency_bootstrap": getattr(mgr, "last_dependency_bootstrap_result", None),
    }


@router.post("/update")
async def update(body: UpdateReq, mgr: ScenarioManager = Depends(_get_manager)):
    if body.async_operation:
        operation = submit_update_operation(
            target_kind="scenario",
            target_id=body.name,
            webspace_id=body.webspace_id,
        )
        return {
            "ok": True,
            "accepted": True,
            "operation_id": operation["operation_id"],
            "operation": operation,
        }
    webspace_id = body.webspace_id or default_webspace_id()
    mgr.sync()
    try:
        dependency_bootstrap = mgr.bootstrap_dependencies(body.name, webspace_id=webspace_id)
    except Exception as exc:
        dependency_bootstrap = {
            "ok": False,
            "scenario_id": body.name,
            "webspace_id": webspace_id,
            "required": [],
            "items": [],
            "succeeded": [],
            "failed": [],
            "error": f"{type(exc).__name__}: {exc}",
        }
    meta = None
    try:
        for item in list(mgr.list_present() or []):
            if _meta_id(item) == body.name:
                meta = item
                break
    except Exception:
        meta = None
    try:
        mgr.sync_to_yjs(body.name, webspace_id=webspace_id, emit_event=False)
    except Exception:
        pass
    try:
        await rebuild_webspace_from_sources(
            webspace_id,
            action="scenario_update_sync",
            scenario_id=body.name,
            source_of_truth="scenario_projection",
        )
    except Exception:
        pass
    return {
        "ok": True,
        "scenario": {
            "id": _meta_id(meta) if meta is not None else body.name,
            "version": getattr(meta, "version", None),
            "path": str(getattr(meta, "path", "")),
        },
        "dependency_bootstrap": dependency_bootstrap,
    }


@router.delete("/{name}")
async def remove(name: str, mgr: ScenarioManager = Depends(_get_manager)):
    mgr.uninstall(name)
    try:
        await rebuild_webspace_from_sources(
            default_webspace_id(),
            action="scenario_uninstall_sync",
            source_of_truth="scenario_projection",
        )
    except Exception:
        pass
    return {"ok": True}

@router.post("/uninstall")
async def uninstall(body: UninstallReq, mgr: ScenarioManager = Depends(_get_manager)):
    mgr.uninstall(body.name)
    try:
        await rebuild_webspace_from_sources(
            body.webspace_id or default_webspace_id(),
            action="scenario_uninstall_sync",
            source_of_truth="scenario_projection",
        )
    except Exception:
        pass
    return {"ok": True}


@router.post("/push")
async def push(body: PushReq, mgr: ScenarioManager = Depends(_get_manager)):
    revision = mgr.push(body.name, body.message, signoff=body.signoff)
    return {"ok": True, "revision": revision}
