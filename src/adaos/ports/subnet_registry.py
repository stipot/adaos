from __future__ import annotations
from dataclasses import dataclass
from typing import Protocol, Dict, Any, List, Optional


@dataclass
class NodeInfo:
    node_id: str
    last_seen: float
    status: str  # "up" | "down"
    meta: Dict[str, Any]


class SubnetRegistryPort(Protocol):
    def register_node(self, node_id: str, meta: Dict[str, Any] | None = None) -> NodeInfo: ...
    def heartbeat(self, node_id: str) -> Optional[NodeInfo]: ...
    def mark_down_if_expired(self) -> List[NodeInfo]: ...
    def list_nodes(self) -> List[NodeInfo]: ...
    def get_node(self, node_id: str) -> Optional[NodeInfo]: ...
    def unregister_node(self, node_id: str) -> bool: ...
