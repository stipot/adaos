from __future__ import annotations
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from typing import Any, Dict, Optional, List

from adaos.sdk.scenario_service import (
    list_installed,
    read_prototype,
    write_prototype,
    create_scenario,
    install_from_repo,
    update_from_repo,
    delete_scenario,
    read_impl,
    write_impl,
    read_bindings,
    write_bindings,
    read_meta,
    pull_scenario,
    install_scenario,
    uninstall_scenario,
    push_scenario,
)
from adaos.agent.core.scenario_engine.store import (
    load_prototype,
    load_impl as _load_impl,
    apply_rewrite,
)
from adaos.agent.core.scenario_engine.dsl import ImplementationRewrite, validate_rewrite
from adaos.agent.core.scenario_engine.runtime import run_scenario, stop_instance, MANAGER, stop_by_activity

router = APIRouter(tags=["scenarios"])

# ---- DevOps: install/create/delete/list ----


@router.get("/list")
async def list_scenarios():
    return {"items": list_installed()}


class CreateReq(BaseModel):
    id: str
    template: Optional[str] = "template"


@router.post("/create")
async def create(body: CreateReq):
    p = create_scenario(body.id, body.template or "template")
    return {"ok": True, "path": str(p)}


class InstallRepoReq(BaseModel):
    repo: str
    sid: Optional[str] = None
    ref: Optional[str] = None
    subpath: Optional[str] = None


class PushReq(BaseModel):
    message: Optional[str] = None


@router.post("/install_repo")
async def install_repo(body: InstallRepoReq):
    p = install_from_repo(body.repo, sid=body.sid, ref=body.ref, subpath=body.subpath)
    return {"ok": True, "path": str(p)}


@router.delete("/{sid}")
async def remove(sid: str):
    ok = delete_scenario(sid)
    if not ok:
        raise HTTPException(404, "not found")
    return {"ok": True}


# meta (источник git)
@router.get("/meta/{sid}")
async def get_meta(sid: str):
    return read_meta(sid)


class UpdateRepoReq(BaseModel):
    ref: Optional[str] = None  # можно переключиться на ветку/тег


@router.post("/update_repo/{sid}")
async def update_repo(sid: str, body: UpdateRepoReq):
    p = update_from_repo(sid, ref=body.ref)
    return {"ok": True, "path": str(p)}


# ---- Prototype / Implementation / Bindings ----


@router.get("/{sid}")
async def get_proto(sid: str):
    return read_prototype(sid)


class ProtoUpdateReq(BaseModel):
    data: Dict[str, Any]


@router.put("/{sid}")
async def update_proto(sid: str, body: ProtoUpdateReq):
    p = write_prototype(sid, body.data)
    return {"ok": True, "path": str(p)}


@router.get("/impl/{user}/{sid}")
async def get_impl(user: str, sid: str):
    return read_impl(sid, user)


@router.patch("/impl/{user}/{sid}")
async def patch_impl(user: str, sid: str, patch: ImplementationRewrite):
    proto = load_prototype(sid)
    validate_rewrite(proto, patch)
    p = write_impl(sid, user, patch.dict(by_alias=True))
    return {"ok": True, "path": str(p)}


@router.get("/bindings/{user}/{sid}")
async def get_bindings(user: str, sid: str):
    return read_bindings(sid, user)


@router.post("/bindings/{user}/{sid}")
async def set_bindings(user: str, sid: str, data: Dict[str, Any]):
    p = write_bindings(sid, user, data)
    return {"ok": True, "path": str(p)}


# ---- Effective / Run / Instances ----


@router.get("/{sid}/effective/{user}")
async def effective(sid: str, user: str):
    proto = load_prototype(sid)
    imp = _load_impl(sid, user)
    eff = apply_rewrite(proto, imp)
    return eff.dict(by_alias=True)


class RunReq(BaseModel):
    user: str
    ioOverride: Optional[Dict[str, Any]] = None


@router.post("/{sid}/run")
async def run(sid: str, body: RunReq):
    iid = await run_scenario(sid, body.user, body.ioOverride)
    return {"ok": True, "iid": iid}


@router.get("/instances")
async def instances():
    return {"items": MANAGER.list()}


@router.post("/instances/{iid}/stop")
async def stop(iid: str):
    ok = await stop_instance(iid)
    if not ok:
        raise HTTPException(404, "instance not found")
    return {"ok": True}


class StopByActivityReq(BaseModel):
    activity: str


@router.post("/instances/stopByActivity")
async def stop_by_act(body: StopByActivityReq):
    await stop_by_activity(body.activity)
    return {"ok": True}


@router.post("/pull/{sid}")
async def pull_scenario_ep(sid: str):
    """
    Подтянуть/обновить сценарий из монорепозитория (sparse-checkout + git pull + валидация).
    """
    msg = pull_scenario(sid)
    return {"ok": True, "message": msg}


@router.post("/install/{sid}")
async def install_scenario_ep(sid: str):
    """
    Установить сценарий (алиас pull): отметит installed=1 и синхронизирует sparse-checkout.
    """
    msg = install_scenario(sid)
    return {"ok": True, "message": msg}


@router.post("/uninstall/{sid}")
async def uninstall_scenario_ep(sid: str):
    """
    Деинсталляция сценария: installed=0 и пересборка sparse-checkout.
    """
    msg = uninstall_scenario(sid)
    return {"ok": True, "message": msg}


@router.post("/push/{sid}")
async def push_scenario_ep(sid: str, body: PushReq):
    msg = push_scenario(sid, body.message)
    return {"ok": True, "message": msg}
