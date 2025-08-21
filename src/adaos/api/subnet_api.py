from __future__ import annotations

from fastapi import APIRouter, HTTPException, Depends
from pydantic import BaseModel
from typing import Any

from adaos.api.auth import require_token
from adaos.agent.core.node_config import load_config
from adaos.agent.core.subnet_context import CTX
from adaos.agent.core.subnet_registry import (
    register_node,
    heartbeat as registry_heartbeat,
    list_nodes as registry_list,
    get_node as registry_get,
    unregister_node as registry_unregister,
    LEASE_SECONDS_DEFAULT,
)

import adaos.sdk.bus as bus

router = APIRouter()


# ---------- Models ----------


class RegisterRequest(BaseModel):
    node_id: str
    subnet_id: str
    hostname: str | None = None
    roles: list[str] | None = None


class RegisterResponse(BaseModel):
    ok: bool
    lease_seconds: int = LEASE_SECONDS_DEFAULT


class HeartbeatRequest(BaseModel):
    node_id: str


class HeartbeatResponse(BaseModel):
    ok: bool
    lease_seconds: int = LEASE_SECONDS_DEFAULT


class CtxValue(BaseModel):
    value: Any


class DeregisterRequest(BaseModel):
    node_id: str


# ---------- Endpoints (hub-only, mounted under /api) ----------


@router.post("/subnet/register", response_model=RegisterResponse, dependencies=[Depends(require_token)])
async def register(body: RegisterRequest):
    """
    Регистрация ноды на hub.
    """
    conf = load_config()
    if conf.role != "hub":
        raise HTTPException(status_code=403, detail="only hub node accepts registrations")

    if body.subnet_id != conf.subnet_id:
        raise HTTPException(status_code=400, detail="subnet mismatch")

    # Добавляем/обновляем запись в реестре
    was = registry_get(body.node_id)
    register_node(
        body.node_id,
        meta={"hostname": body.hostname, "roles": body.roles or []},
    )

    # Сигнализируем о появлении ноды (node.up)
    if was is None or (isinstance(was, dict) and was.get("status") != "up"):
        await bus.emit("net.subnet.node.up", {"node_id": body.node_id}, source="subnet_api", actor="system")

    return RegisterResponse(ok=True, lease_seconds=LEASE_SECONDS_DEFAULT)


@router.post("/subnet/heartbeat", response_model=HeartbeatResponse, dependencies=[Depends(require_token)])
async def heartbeat(body: HeartbeatRequest):
    """
    Heartbeat от ноды к hub. Обновляет last_seen и (если надо) возвращает статус в 'up'.
    """
    conf = load_config()
    if conf.role != "hub":
        raise HTTPException(status_code=403, detail="only hub node accepts heartbeats")

    before = registry_get(body.node_id)
    info = registry_heartbeat(body.node_id)
    if not info:
        # Неизвестная нода — просим повторную регистрацию
        raise HTTPException(status_code=404, detail="node not registered")

    # Если статус был 'down' и поднялся в 'up' — шлём node.up
    if before and isinstance(before, dict) and before.get("status") == "down" and info.status == "up":
        await bus.emit("net.subnet.node.up", {"node_id": body.node_id}, source="subnet_api", actor="system")

    return HeartbeatResponse(ok=True, lease_seconds=LEASE_SECONDS_DEFAULT)


@router.post("/subnet/deregister", dependencies=[Depends(require_token)])
async def deregister(body: DeregisterRequest):
    """Корректная дерегистрация ноды на hub (когда нода уходит из подсети)."""
    conf = load_config()
    if conf.role != "hub":
        raise HTTPException(status_code=403, detail="only hub node accepts deregistration")
    existed = registry_unregister(body.node_id)
    if existed:
        await bus.emit("net.subnet.node.down", {"node_id": body.node_id}, source="subnet_api", actor="system")
    return {"ok": True, "existed": bool(existed)}


@router.get("/subnet/context/{key}", dependencies=[Depends(require_token)])
async def ctx_get(key: str):
    """
    Получение значения глобального контекста подсети (hub-only).
    """
    conf = load_config()
    if conf.role != "hub":
        raise HTTPException(status_code=403, detail="only hub node serves context")
    return {"ok": True, "value": CTX.hub_get(key)}


@router.put("/subnet/context/{key}", dependencies=[Depends(require_token)])
async def ctx_set(key: str, body: CtxValue):
    """
    Запись значения в глобальный контекст подсети (hub-only).
    """
    conf = load_config()
    if conf.role != "hub":
        raise HTTPException(status_code=403, detail="only hub node serves context")
    CTX.hub_set(key, body.value)
    return {"ok": True}


@router.get("/subnet/nodes", dependencies=[Depends(require_token)])
async def nodes_list():
    """
    Список нод подсети с их статусами (hub-only).
    """
    conf = load_config()
    if conf.role != "hub":
        raise HTTPException(status_code=403, detail="only hub node lists nodes")
    return {"ok": True, "nodes": registry_list()}


@router.get("/subnet/nodes/{node_id}", dependencies=[Depends(require_token)])
async def node_get(node_id: str):
    """
    Детали по конкретной ноде (hub-only).
    """
    conf = load_config()
    if conf.role != "hub":
        raise HTTPException(status_code=403, detail="only hub node has node details")
    info = registry_get(node_id)
    if not info:
        raise HTTPException(status_code=404, detail="node not found")
    return {"ok": True, "node": info}
