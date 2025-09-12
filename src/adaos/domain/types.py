# src\adaos\domain\types.py
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
