from __future__ import annotations
from typing import Any
from dataclasses import dataclass
from adaos.services.settings import Settings
from adaos.ports import EventBus, Process, Capabilities, Devices, KV, SQL, Secrets, Net, Updates, GitClient
from adaos.ports.paths import PathProvider
from adaos.ports.fs import FSPolicy
from adaos.ports.sandbox import Sandbox


@dataclass(slots=True)
class AgentContext:
    settings: Settings
    paths: PathProvider
    bus: EventBus
    proc: Process
    caps: Capabilities
    devices: Devices
    kv: KV
    sql: SQL
    secrets: Secrets
    net: Net
    updates: Updates
    git: GitClient
    fs: FSPolicy
    sandbox: Sandbox
