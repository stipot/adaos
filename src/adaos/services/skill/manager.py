from __future__ import annotations
import re
from pathlib import Path
from typing import Optional

from adaos.domain import SkillMeta, SkillRecord
from adaos.ports import EventBus, GitClient, SkillRepository, SkillRegistry
from adaos.ports.paths import PathProvider
from adaos.services.eventbus import emit

_name_re = re.compile(r"^[a-zA-Z0-9_\-\/]+$")


class SkillManager:
    """
    Управляет установленными навыками:
      - источник истины: SkillRegistry (SQL)
      - git sparse-checkout: выставляем набор путей = установленным навыкам
      - list/install/remove/sync работают ТОЛЬКО по реестру
    """

    def __init__(self, *, repo: SkillRepository, registry: SkillRegistry, git: GitClient, paths: PathProvider, bus: EventBus):
        self.repo = repo
        self.reg = registry
        self.git = git
        self.paths = paths
        self.bus = bus

    # ------ read-only ------
    def list_installed(self) -> list[SkillRecord]:
        return self.reg.list()

    def list_present(self) -> list[SkillMeta]:
        self.repo.ensure()
        return self.repo.list()

    # ------ actions ------
    def sync(self) -> None:
        self.repo.ensure()  # <-- добавили
        root = self.paths.skills_dir()
        names = [r.name for r in self.reg.list()]
        self.git.sparse_init(root, cone=False)
        if names:
            self.git.sparse_set(root, names, no_cone=True)
        self.git.pull(root)
        emit(self.bus, "skill.sync", {"count": len(names)}, "skill.mgr")

    def install(self, name: str, *, pin: Optional[str] = None) -> SkillMeta:
        name = name.strip()
        if not _name_re.match(name):
            raise ValueError("invalid skill name")

        self.repo.ensure()  # <-- добавили

        self.reg.register(name, pin=pin)
        try:
            root = self.paths.skills_dir()
            names = [r.name for r in self.reg.list()]
            self.git.sparse_init(root, cone=False)
            self.git.sparse_set(root, names, no_cone=True)
            self.git.pull(root)
            meta = self.repo.get(name)
            if not meta:
                raise FileNotFoundError(f"skill '{name}' not found in monorepo")
            emit(self.bus, "skill.installed", {"id": meta.id.value, "pin": pin}, "skill.mgr")
            return meta
        except Exception:
            self.reg.unregister(name)
            raise

    def remove(self, name: str) -> None:
        self.repo.ensure()  # <-- добавили
        self.reg.unregister(name)
        root = self.paths.skills_dir()
        names = [r.name for r in self.reg.list()]
        self.git.sparse_init(root, cone=False)
        if names:
            self.git.sparse_set(root, names, no_cone=True)
        self.git.pull(root)
        p = Path(root) / name
        if p.exists():
            import shutil

            shutil.rmtree(p, ignore_errors=True)
        emit(self.bus, "skill.removed", {"id": name}, "skill.mgr")
