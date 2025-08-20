# src/adaos/agent/core/subnet_registry.py
from __future__ import annotations
from dataclasses import dataclass, asdict
from typing import Dict, Any, List
import time

LEASE_SECONDS_DEFAULT = 10
DOWN_GRACE_SECONDS = 2 * LEASE_SECONDS_DEFAULT  # через сколько после последнего heartbeat считаем ноду down


@dataclass
class NodeInfo:
    node_id: str
    last_seen: float
    status: str  # "up" | "down"
    meta: Dict[str, Any]


# in‑memory (hub)
_REGISTRY: Dict[str, NodeInfo] = {}


def register_node(node_id: str, meta: Dict[str, Any] | None = None) -> NodeInfo:
    now = time.time()
    meta = meta or {}
    prev = _REGISTRY.get(node_id)
    info = NodeInfo(node_id=node_id, last_seen=now, status="up", meta=meta)
    _REGISTRY[node_id] = info
    return info


def heartbeat(node_id: str) -> NodeInfo | None:
    info = _REGISTRY.get(node_id)
    if not info:
        return None
    info.last_seen = time.time()
    if info.status != "up":
        info.status = "up"
    return info


def mark_down_if_expired() -> List[NodeInfo]:
    """Проверить таймауты, вернуть список нод, у которых статус изменился на 'down'."""
    now = time.time()
    changed: List[NodeInfo] = []
    for info in _REGISTRY.values():
        if info.status == "up" and (now - info.last_seen) > DOWN_GRACE_SECONDS:
            info.status = "down"
            changed.append(info)
    return changed


def list_nodes() -> List[Dict[str, Any]]:
    return [asdict(n) for n in _REGISTRY.values()]


def get_node(node_id: str) -> Dict[str, Any] | None:
    info = _REGISTRY.get(node_id)
    return asdict(info) if info else None


def unregister_node(node_id: str) -> bool:
    return _REGISTRY.pop(node_id, None) is not None
