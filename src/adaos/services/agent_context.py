from __future__ import annotations
from dataclasses import dataclass
# PR-2 will supply Settings and PathProvider
from typing import Any
from adaos.ports import EventBus, Process, Capabilities, Devices, KV, SQL, Secrets, Net, Updates

@dataclass(slots=True)
class AgentContext:
    settings: Any
    bus: EventBus
    proc: Process
    caps: Capabilities
    devices: Devices
    kv: KV
    sql: SQL
    secrets: Secrets
    net: Net
    updates: Updates
