from __future__ import annotations
from dataclasses import dataclass
from typing import Any
from adaos.ports import EventBus, Process, Capabilities, Devices, KV, SQL, Secrets, Net, Updates


@dataclass(slots=True)
class AgentContext:
    # Settings добавим в PR-2 (отдельный класс), пока тип Any чтобы не ломать порядок
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
