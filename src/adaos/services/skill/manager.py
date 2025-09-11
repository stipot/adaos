# src\adaos\services\skill\manager.py
from __future__ import annotations
import re, os
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
from adaos.services.agent_context import get_ctx
from adaos.services.skill.validation import SkillValidationService
from adaos.services.agent_context import AgentContext

_name_re = re.compile(r"^[a-zA-Z0-9_\-\/]+$")


class SkillManager:
    def __init__(
        self,
        *,
        git: GitClient,  # Deprecated. TODO Move to ctx
        paths: PathProvider,  # Deprecated. TODO Move to ctx
        caps: Capabilities,
        settings: Settings | None = None,
        registry: SkillRegistry = None,
        repo: SkillRepository | None = None,  # Deprecated. TODO Move to ctx
        bus: EventBus | None = None,
    ):
        self.reg = registry
        self.bus = bus
        self.caps = caps
        self.settings = settings
        self.ctx: AgentContext = get_ctx()

    def list_installed(self) -> list[SkillRecord]:
        self.caps.require("core", "skills.manage")
        return self.ctx.skills_repo.list()

    def list_present(self) -> list[SkillMeta]:
        self.caps.require("core", "skills.manage")
        self.ctx.skills_repo.ensure()
        return self.ctx.skills_repo.list()

    def get(self, skill_id: str) -> Optional[SkillMeta]:
        return self.ctx.skills_repo.get(skill_id)

    def sync(self) -> None:
        self.caps.require("core", "skills.manage", "net.git")
        self.ctx.skills_repo.ensure()
        root = self.ctx.paths.skills_dir()
        names = [r.name for r in self.reg.list()]
        ensure_clean(self.ctx.git, root, names)
        self.ctx.git.sparse_init(root, cone=False)
        if names:
            self.ctx.git.sparse_set(root, names, no_cone=True)
        self.ctx.git.pull(root)
        emit(self.bus, "skill.sync", {"count": len(names)}, "skill.mgr")

    def install(self, name: str, pin: str | None = None, validate: bool = True, strict: bool = True, probe_tools: bool = False) -> tuple[SkillMeta, Optional[object]]:
        """
        Возвращает (meta, report|None). При strict и ошибках валидации можно выбрасывать исключение.
        """
        self.caps.require("core", "skills.manage")
        name = name.strip()
        if not _name_re.match(name):
            raise ValueError("invalid skill name")

        # 1) регистрируем (идемпотентно)
        self.reg.register(name, pin=pin)
        # 2) в тестах/без .git — только реестр
        root = self.ctx.paths.skills_dir()
        test_mode = os.getenv("ADAOS_TESTING") == "1"
        if test_mode:
            return f"installed: {name} (registry-only{' test-mode' if test_mode else ''})"
        # 3) mono-only установка через репозиторий (sparse-add + pull)
        meta = self.ctx.skills_repo.install(name, branch=None)
        """ if not validate:
            return meta, None """
        report = SkillValidationService(self.ctx).validate(meta.id.value, strict=strict, probe_tools=probe_tools)
        if strict and not report.ok:
            # опционально можно откатывать установку:
            # self.ctx.skills_repo.uninstall(meta.id.value)
            # и/или пробрасывать исключение
            return meta, report

        return meta, report  # return f"installed: {name}"

    def uninstall(self, name: str) -> None:
        self.caps.require("core", "skills.manage", "net.git")
        name = name.strip()
        if not _name_re.match(name):
            raise ValueError("invalid skill name")
        # если записи нет — считаем idempotent
        rec = self.reg.get(name)
        if not rec:
            return f"uninstalled: {name} (not found)"
        self.reg.unregister(name)
        root = self.ctx.paths.skills_dir()
        test_mode = os.getenv("ADAOS_TESTING") == "1"
        # в тестах/без .git — только реестр, без git операций
        if test_mode or not (root / ".git").exists():
            return f"uninstalled: {name} (registry-only{' test-mode' if test_mode else ''})"
        names = [r.name for r in self.reg.list()]
        ensure_clean(self.ctx.git, root, names)
        self.ctx.git.sparse_init(root, cone=False)
        if names:
            self.ctx.git.sparse_set(root, names, no_cone=True)
        self.ctx.git.pull(root)
        remove_tree(str(Path(root) / name), fs=self.ctx.paths.ctx.fs if hasattr(self.ctx.paths, "ctx") else get_ctx().fs)
        emit(self.bus, "skill.uninstalled", {"id": name}, "skill.mgr")

    def push(self, name: str, message: str, *, signoff: bool = False) -> str:
        self.caps.require("core", "skills.manage", "git.write", "net.git")
        root = self.ctx.paths.skills_dir()
        if not (root / ".git").exists():
            raise RuntimeError("Skills repo is not initialized. Run `adaos skill sync` once.")

        sub = name.strip()
        changed = self.ctx.git.changed_files(root, subpath=sub)
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
        sha = self.ctx.git.commit_subpath(root, subpath=name.strip(), message=msg, author_name=author_name, author_email=author_email, signoff=signoff)
        if sha != "nothing-to-commit":
            self.ctx.git.push(root)
        return sha
