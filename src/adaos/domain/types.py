from __future__ import annotations
from dataclasses import dataclass
from typing import Any, Mapping


@dataclass(frozen=True, slots=True)
class SkillId:
    value: str


@dataclass(frozen=True, slots=True)
class ScenarioId:
    value: str


@dataclass(frozen=True, slots=True)
class Event:
    type: str
    payload: Mapping[str, Any]
    source: str
    ts: float  # unix time, seconds


@dataclass(frozen=True, slots=True)
class ProcessSpec:
    name: str
    # либо внешний процесс (cmd), либо внутренняя корутина (entrypoint)
    cmd: list[str] | None = None
    entrypoint: Any | None = None  # Callable[..., Awaitable[None]]
    env: Mapping[str, str] | None = None
    capabilities: frozenset[str] = frozenset()
