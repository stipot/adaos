from __future__ import annotations
import time
from typing import Dict, Any, List, Optional
from adaos.ports.subnet_registry import SubnetRegistryPort, NodeInfo

LEASE_SECONDS_DEFAULT = 10
DOWN_GRACE_SECONDS = 2 * LEASE_SECONDS_DEFAULT


class InMemorySubnetRegistry(SubnetRegistryPort):
    def __init__(self) -> None:
        self._reg: Dict[str, NodeInfo] = {}

    def register_node(self, node_id: str, meta: Dict[str, Any] | None = None) -> NodeInfo:
        info = NodeInfo(node_id=node_id, last_seen=time.time(), status="up", meta=meta or {})
        self._reg[node_id] = info
        return info

    def heartbeat(self, node_id: str) -> Optional[NodeInfo]:
        info = self._reg.get(node_id)
        if not info:
            return None
        info.last_seen = time.time()
        if info.status != "up":
            info.status = "up"
        return info

    def mark_down_if_expired(self) -> List[NodeInfo]:
        now = time.time()
        changed: List[NodeInfo] = []
        for info in self._reg.values():
            if info.status == "up" and (now - info.last_seen) > DOWN_GRACE_SECONDS:
                info.status = "down"
                changed.append(info)
        return changed

    def list_nodes(self) -> List[NodeInfo]:
        return list(self._reg.values())

    def get_node(self, node_id: str) -> Optional[NodeInfo]:
        return self._reg.get(node_id)

    def unregister_node(self, node_id: str) -> bool:
        return self._reg.pop(node_id, None) is not None


# singleton
_REGISTRY: InMemorySubnetRegistry | None = None


def get_subnet_registry() -> InMemorySubnetRegistry:
    global _REGISTRY
    if _REGISTRY is None:
        _REGISTRY = InMemorySubnetRegistry()
    return _REGISTRY
