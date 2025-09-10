# \src\adaos\services\skill\service.py
from __future__ import annotations
from typing import Optional
from adaos.domain import SkillMeta
from adaos.ports import EventBus
from adaos.ports.skills import SkillRepository
from adaos.services.eventbus import emit


class SkillService:
    def __init__(self, repo: SkillRepository, bus: EventBus):
        self.repo = repo
        self.bus = bus

    def list(self) -> list[SkillMeta]:
        return self.repo.list()

    def install(self, name: str, *, branch: Optional[str] = None, dest_name: Optional[str] = None) -> SkillMeta:
        meta = self.repo.install(name, branch=branch, dest_name=dest_name)
        emit(self.bus, "skill.installed", {"id": meta.id.value, "version": meta.version, "path": meta.path}, "skill.svc")
        return meta

    def uninstall(self, skill_id: str) -> None:
        self.repo.uninstall(skill_id)
        emit(self.bus, "skill.uninstalled", {"id": skill_id}, "skill.svc")
