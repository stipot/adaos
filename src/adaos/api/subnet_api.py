from __future__ import annotations
from fastapi import APIRouter, HTTPException, Depends
from pydantic import BaseModel
from typing import Dict, Any
import time

from adaos.api.auth import require_token
from adaos.agent.core.node_config import load_config
from adaos.agent.core.subnet_context import CTX

router = APIRouter()

# простейший реестр узлов в памяти hub-ноды
# { node_id: { "last_seen": ts, "meta": {...} } }
_NODE_REGISTRY: Dict[str, Dict[str, Any]] = {}


class RegisterRequest(BaseModel):
    node_id: str
    subnet_id: str
    hostname: str | None = None
    roles: list[str] | None = None


class RegisterResponse(BaseModel):
    ok: bool
    lease_seconds: int = 10


class HeartbeatRequest(BaseModel):
    node_id: str


class HeartbeatResponse(BaseModel):
    ok: bool
    lease_seconds: int = 10


class CtxValue(BaseModel):
    value: Any


@router.post("/subnet/register", response_model=RegisterResponse, dependencies=[Depends(require_token)])
async def register(body: RegisterRequest):
    conf = load_config()
    if conf.role != "hub":
        raise HTTPException(403, "only hub node accepts registrations")

    if body.subnet_id != conf.subnet_id:
        raise HTTPException(400, "subnet mismatch")

    _NODE_REGISTRY[body.node_id] = {
        "last_seen": time.time(),
        "meta": {"hostname": body.hostname, "roles": body.roles or []},
    }
    return RegisterResponse(ok=True, lease_seconds=10)


@router.post("/subnet/heartbeat", response_model=HeartbeatResponse, dependencies=[Depends(require_token)])
async def heartbeat(body: HeartbeatRequest):
    conf = load_config()
    if conf.role != "hub":
        raise HTTPException(403, "only hub node accepts heartbeats")

    node = _NODE_REGISTRY.get(body.node_id)
    if not node:
        # неизвестный узел — попросим повторную регистрацию
        raise HTTPException(404, "node not registered")
    node["last_seen"] = time.time()
    return HeartbeatResponse(ok=True, lease_seconds=10)


@router.get("/subnet/context/{key}", dependencies=[Depends(require_token)])
async def ctx_get(key: str):
    conf = load_config()
    if conf.role != "hub":
        raise HTTPException(403, "only hub node serves context (for now)")
    return {"ok": True, "value": CTX.hub_get(key)}


@router.put("/subnet/context/{key}", dependencies=[Depends(require_token)])
async def ctx_set(key: str, body: CtxValue):
    conf = load_config()
    if conf.role != "hub":
        raise HTTPException(403, "only hub node serves context (for now)")
    CTX.hub_set(key, body.value)
    return {"ok": True}
