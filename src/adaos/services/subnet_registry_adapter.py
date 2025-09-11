from __future__ import annotations
from typing import List, Any

from adaos.ports.subnet import SubnetRegistryPort
from adaos.agent.core.subnet_registry import mark_down_if_expired as _mark_down_if_expired


class SubnetRegistryAdapter(SubnetRegistryPort):
    def mark_down_if_expired(self) -> List[Any]:
        return _mark_down_if_expired()
