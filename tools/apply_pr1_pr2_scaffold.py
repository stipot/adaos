import os, textwrap

FILES = {
    "src/adaos/domain/types.py": """
    from __future__ import annotations
    from dataclasses import dataclass
    from typing import Any, Mapping

    @dataclass(frozen=True, slots=True)
    class SkillId: value: str

    @dataclass(frozen=True, slots=True)
    class ScenarioId: value: str

    @dataclass(frozen=True, slots=True)
    class Event:
        type: str
        payload: Mapping[str, Any]
        source: str
        ts: float

    @dataclass(frozen=True, slots=True)
    class ProcessSpec:
        name: str
        cmd: list[str] | None = None
        entrypoint: Any | None = None
        env: Mapping[str, str] | None = None
        capabilities: frozenset[str] = frozenset()
    """,
    "src/adaos/domain/__init__.py": """
    from .types import SkillId, ScenarioId, Event, ProcessSpec
    __all__ = ["SkillId", "ScenarioId", "Event", "ProcessSpec"]
    """,
    "src/adaos/ports/contracts.py": """
    from __future__ import annotations
    from typing import Protocol, Iterable, Mapping, Any, Callable
    from adaos.domain import Event, ProcessSpec, SkillId

    class EventBus(Protocol):
        def publish(self, event: Event) -> None: ...
        def subscribe(self, type_prefix: str, handler: Callable[[Event], None]) -> None: ...

    class Process(Protocol):
        async def start(self, spec: ProcessSpec) -> str: ...
        async def stop(self, handle: str, timeout_s: float = 5.0) -> None: ...
        async def status(self, handle: str) -> str: ...

    class Capabilities(Protocol):
        def check(self, subject: str, required: Iterable[str]) -> bool: ...
        def grant(self, subject: str, caps: Iterable[str]) -> None: ...
        def revoke(self, subject: str, caps: Iterable[str]) -> None: ...

    class Devices(Protocol):
        def list(self, typ: str | None = None) -> list[dict[str, Any]]: ...
        def acquire(self, typ: str, policy: Mapping[str, Any]) -> dict[str, Any]: ...
        def release(self, device_id: str) -> None: ...

    class KV(Protocol):
        def get(self, key: str, default: Any = None) -> Any: ...
        def set(self, key: str, value: Any) -> None: ...
        def delete(self, key: str) -> None: ...

    class SQL(Protocol):
        def connect(self) -> Any: ...

    class Secrets(Protocol):
        def put(self, name: str, value: bytes, scope: str) -> None: ...
        def get(self, name: str, scope: str) -> bytes: ...

    class Net(Protocol):
        def allowed(self, subject: str, host: str, port: int) -> bool: ...

    class Updates(Protocol):
        def fetch(self, id: str, version: str, source: str) -> str: ...
        def verify(self, artifact_path: str) -> bool: ...
        def install(self, artifact_path: str) -> None: ...
        def rollback(self, id: str, version: str) -> None: ...
    """,
    "src/adaos/ports/__init__.py": """
    from .contracts import (
        EventBus, Process, Capabilities, Devices, KV, SQL, Secrets, Net, Updates
    )
    __all__ = ["EventBus", "Process", "Capabilities", "Devices", "KV", "SQL", "Secrets", "Net", "Updates"]
    """,
    "src/adaos/services/agent_context.py": """
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
    """,
    # PR-2
    "src/adaos/services/settings.py": """
    from __future__ import annotations
    from dataclasses import dataclass
    import os

    @dataclass(slots=True)
    class Settings:
        base_dir: str
        profile: str = "default"

        @staticmethod
        def from_env() -> "Settings":
            base = os.environ.get("ADAOS_BASE_DIR") or os.path.expanduser("~/.adaos")
            profile = os.environ.get("ADAOS_PROFILE", "default")
            return Settings(base_dir=base, profile=profile)
    """,
    "src/adaos/ports/paths.py": """
    from __future__ import annotations
    from typing import Protocol

    class PathProvider(Protocol):
        def base(self) -> str: ...
        def skills_dir(self) -> str: ...
        def scenarios_dir(self) -> str: ...
        def state_dir(self) -> str: ...
        def cache_dir(self) -> str: ...
        def logs_dir(self) -> str: ...
    """,
    "src/adaos/adapters/fs/path_provider.py": """
    from __future__ import annotations
    import os
    from typing import Final
    from adaos.ports.paths import PathProvider
    from adaos.services.settings import Settings

    class LocalPathProvider(PathProvider):
        def __init__(self, settings: Settings):
            self._s: Final[Settings] = settings

        def _mk(self, *parts: str) -> str:
            p = os.path.join(*parts)
            os.makedirs(p, exist_ok=True)
            return p

        def base(self) -> str: return self._mk(self._s.base_dir)
        def skills_dir(self) -> str: return self._mk(self.base(), "skills")
        def scenarios_dir(self) -> str: return self._mk(self.base(), "scenarios")
        def state_dir(self) -> str: return self._mk(self.base(), "state")
        def cache_dir(self) -> str: return self._mk(self.base(), "cache")
        def logs_dir(self) -> str: return self._mk(self.base(), "logs")
    """,
    # patched AgentContext for PR-2
    "src/adaos/services/_agent_context_PR2_patch.txt": """
    from __future__ import annotations
    from dataclasses import dataclass
    from adaos.services.settings import Settings
    from adaos.ports import EventBus, Process, Capabilities, Devices, KV, SQL, Secrets, Net, Updates
    from adaos.ports.paths import PathProvider

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
    """,
}


def write(path: str, content: str):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(textwrap.dedent(content).lstrip())


def main():
    for p, c in FILES.items():
        write(p, c)
    print("PR-1..2 scaffold written.")
    print("Now replace src/adaos/services/agent_context.py with the PR-2 patch when you switch to PR-2.")
    print("Tip: set ADAOS_BASE_DIR to test path provider, e.g. on Windows PowerShell:")
    print('$env:ADAOS_BASE_DIR="C:\\\\adaos_base_test"')


if __name__ == "__main__":
    main()
