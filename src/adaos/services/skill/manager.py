from __future__ import annotations
import re
from pathlib import Path
from typing import Optional

from adaos.domain import SkillMeta, SkillRecord
from adaos.ports import EventBus, GitClient, SkillRepository, SkillRegistry
from adaos.ports.paths import PathProvider
from adaos.services.eventbus import emit
from adaos.ports import Capabilities
from adaos.services.fs.safe_io import remove_tree
from adaos.services.git.safe_commit import sanitize_message, check_no_denied
from adaos.services.git.workspace_guard import ensure_clean
from adaos.services.settings import Settings
from adaos.apps.bootstrap import get_ctx

_name_re = re.compile(r"^[a-zA-Z0-9_\-\/]+$")


class SkillManager:
    def __init__(self, *, repo: SkillRepository, registry: SkillRegistry, git: GitClient, paths: PathProvider, bus: EventBus, caps: Capabilities, settings: Settings | None = None):
        self.repo, self.reg, self.git, self.paths, self.bus, self.caps = repo, registry, git, paths, bus, caps
        self.settings = settings

    def list_installed(self) -> list[SkillRecord]:
        self.caps.require("core", "skills.manage")
        return self.reg.list()

    def list_present(self) -> list[SkillMeta]:
        self.caps.require("core", "skills.manage")
        self.repo.ensure()
        return self.repo.list()

    def sync(self) -> None:
        self.caps.require("core", "skills.manage", "net.git")
        self.repo.ensure()
        root = self.paths.skills_dir()
        names = [r.name for r in self.reg.list()]
        ensure_clean(self.git, root, names)
        self.git.sparse_init(root, cone=False)
        if names:
            self.git.sparse_set(root, names, no_cone=True)
        self.git.pull(root)
        emit(self.bus, "skill.sync", {"count": len(names)}, "skill.mgr")

    def install(self, name: str, *, pin: Optional[str] = None) -> SkillMeta:
        self.caps.require("core", "skills.manage", "net.git")
        name = name.strip()
        if not _name_re.match(name):
            raise ValueError("invalid skill name")

        self.repo.ensure()

        self.reg.register(name, pin=pin)
        try:
            root = self.paths.skills_dir()
            names = [r.name for r in self.reg.list()]
            ensure_clean(self.git, root, names)
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
        self.caps.require("core", "skills.manage", "net.git")
        self.repo.ensure()
        self.reg.unregister(name)
        root = self.paths.skills_dir()
        names = [r.name for r in self.reg.list()]
        ensure_clean(self.git, root, names)
        self.git.sparse_init(root, cone=False)
        if names:
            self.git.sparse_set(root, names, no_cone=True)
        self.git.pull(root)
        remove_tree(str(Path(root) / name), fs=self.paths.ctx.fs if hasattr(self.paths, "ctx") else get_ctx().fs)
        emit(self.bus, "skill.removed", {"id": name}, "skill.mgr")

    def push(self, name: str, message: str, *, signoff: bool = False) -> str:
        self.caps.require("core", "skills.manage", "git.write", "net.git")
        root = self.paths.skills_dir()
        if not (Path(root) / ".git").exists():
            raise RuntimeError("Skills repo is not initialized. Run `adaos skill sync` once.")

        sub = name.strip()
        changed = self.git.changed_files(root, subpath=sub)
        if not changed:
            return "nothing-to-push"
        bad = check_no_denied(changed)
        if bad:
            raise PermissionError(f"push denied: sensitive files matched: {', '.join(bad)}")
        # безопасно получаем автора
        if self.settings:
            author_name = self.settings.git_author_name
            author_email = self.settings.git_author_email
        else:
            # fallback, если кто-то создаст менеджер без settings
            try:
                ctx = get_ctx()
                author_name = ctx.settings.git_author_name
                author_email = ctx.settings.git_author_email
            except Exception:
                author_name, author_email = "AdaOS Bot", "bot@adaos.local"
        msg = sanitize_message(message)
        sha = self.git.commit_subpath(root, subpath=name.strip(), message=msg, author_name=author_name, author_email=author_email, signoff=signoff)
        if sha != "nothing-to-commit":
            self.git.push(root)
        return sha
