# src\adaos\services\skill\context.py
from __future__ import annotations
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from adaos.services.agent_context import AgentContext
from adaos.ports.skill_context import CurrentSkill


@dataclass(slots=True)
class SkillContextService:
    ctx: AgentContext

    def set_current_skill(self, name: str) -> bool:
        meta = self.ctx.skills_repo.get(name)
        if not meta:
            return False
        return self.ctx.skill_ctx.set(name, Path(meta.path))

    def clear_current_skill(self) -> None:
        self.ctx.skill_ctx.clear()

    def get_current_skill(self) -> Optional[CurrentSkill]:
        return self.ctx.skill_ctx.get()
