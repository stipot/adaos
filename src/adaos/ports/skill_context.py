from __future__ import annotations
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Protocol


@dataclass(slots=True)
class CurrentSkill:
    name: str
    path: Path


class SkillContextPort(Protocol):
    """src/adaos/services/skill/context.py"""

    def set(self, name: str, path: Path) -> bool: ...
    def clear(self) -> None: ...
    def get(self) -> Optional[CurrentSkill]: ...
