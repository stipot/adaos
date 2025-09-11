from __future__ import annotations
from typing import Protocol, List, Any


class SubnetRegistryPort(Protocol):
    def mark_down_if_expired(self) -> List[Any]: ...
