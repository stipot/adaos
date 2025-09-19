from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from adaos.domain import SkillMeta
from adaos.services.agent_context import AgentContext


@dataclass(slots=True)
class SkillLifecycleService:
    ctx: AgentContext

    def list_installed(self) -> list[SkillMeta]:
        repo = self.ctx.skills_repo
        repo.ensure()
        return repo.list()

    def install(self, ref: str, *, branch: Optional[str] = None, dest_name: Optional[str] = None) -> SkillMeta:
        repo = self.ctx.skills_repo
        return repo.install(ref, branch=branch, dest_name=dest_name)

    def uninstall(self, skill_id: str) -> bool:
        repo = self.ctx.skills_repo
        try:
            repo.uninstall(skill_id)
            return True
        except FileNotFoundError:
            return False
