# src\adaos\adapters\sdk\inproc_skill_context.py
from __future__ import annotations
from contextvars import ContextVar
from pathlib import Path
from typing import Optional

from adaos.ports.skill_context import SkillContextPort, CurrentSkill

_current_skill: ContextVar[Optional[CurrentSkill]] = ContextVar("adaos_current_skill", default=None)


class InprocSkillContext(SkillContextPort):
    def set(self, name: str, path: Path) -> bool:
        if not path.exists():
            return False
        _current_skill.set(CurrentSkill(name=name, path=path))
        return True

    def clear(self) -> None:
        _current_skill.set(None)

    def get(self) -> Optional[CurrentSkill]:
        return _current_skill.get()
