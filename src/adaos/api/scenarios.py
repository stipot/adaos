from __future__ import annotations
from typing import Any, Dict, Optional
from fastapi import APIRouter, Depends
from pydantic import BaseModel

from adaos.api.auth import require_token
from adaos.services.agent_context import get_ctx, AgentContext
from adaos.services.scenario.manager import ScenarioManager
from adaos.adapters.db import SqliteScenarioRegistry


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


# --- API (тонкий фасад CLI) --------------------------------------------------
class InstallReq(BaseModel):
    name: str
    pin: Optional[str] = None


@router.get("/list")
async def list_scenarios(fs: bool = False, mgr: ScenarioManager = Depends(_get_manager)):
    rows = mgr.list_installed()
    items = [_to_mapping(r) for r in (rows or [])]
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
    meta = mgr.install(body.name, pin=body.pin)
    # приведём к компактному виду как в CLI-эхо
    return {
        "ok": True,
        "scenario": {
            "id": _meta_id(meta),
            "version": getattr(meta, "version", None),
            "path": str(getattr(meta, "path", "")),
        },
    }


@router.delete("/{name}")
async def remove(name: str, mgr: ScenarioManager = Depends(_get_manager)):
    mgr.remove(name)
    return {"ok": True}
