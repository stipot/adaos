# src/adaos/services/agent_context.py
from __future__ import annotations
from dataclasses import dataclass, field
from typing import Any, Optional, TYPE_CHECKING

from adaos.services.settings import Settings
from adaos.ports import EventBus, Process, Capabilities, Devices, KV, SQL, Secrets, Net, Updates, GitClient
from adaos.ports.paths import PathProvider
from adaos.ports.fs import FSPolicy
from adaos.ports.sandbox import Sandbox

from adaos.adapters.skills.git_repo import GitSkillRepository
from adaos.adapters.scenarios.git_repo import GitScenarioRepository
from adaos.ports.skill_context import SkillContextPort
from adaos.adapters.sdk.inproc_skill_context import InprocSkillContext
from contextvars import ContextVar
from contextlib import contextmanager

_CTX: ContextVar[Optional[AgentContext]] = ContextVar("adaos_agent_ctx", default=None)


def set_ctx(ctx: AgentContext) -> None:
    """Устанавливает текущий AgentContext (делает доступным через get_ctx)."""
    _CTX.set(ctx)


def get_ctx() -> AgentContext:
    """Возвращает текущий AgentContext или бросает ошибку, если не инициализирован."""
    ctx = _CTX.get()
    if ctx is None:
        raise RuntimeError("AgentContext is not initialized. Call set_ctx(...) during app bootstrap.")
    return ctx


def clear_ctx() -> None:
    """Очищает текущий контекст (для тестов/завершения)."""
    _CTX.set(None)


@contextmanager
def use_ctx(ctx: AgentContext):
    """Временная подмена контекста (удобно в тестах)."""
    token = _CTX.set(ctx)
    try:
        yield
    finally:
        _CTX.reset(token)


if TYPE_CHECKING:
    from adaos.services.i18n.service import I18nService


@dataclass(slots=True)
class AgentContext:
    settings: Settings
    paths: PathProvider
    bus: EventBus
    proc: Process
    caps: Capabilities
    devices: Devices
    kv: KV
    sql: SQL
    secrets: Secrets
    net: Net
    updates: Updates
    git: GitClient
    fs: FSPolicy
    sandbox: Sandbox
    _i18n: Optional[I18nService] = field(default=None, init=False, repr=False)

    # приватные кэши под slots
    _skills_repo: Optional[GitSkillRepository] = field(default=None, init=False, repr=False)
    _scenarios_repo: Optional[GitScenarioRepository] = field(default=None, init=False, repr=False)
    _skill_ctx_port: Optional[SkillContextPort] = field(default=None, init=False, repr=False)

    @property
    def skills_repo(self) -> GitSkillRepository:
        repo = self._skills_repo
        if repo is None:
            repo = GitSkillRepository(
                paths=self.paths,
                git=self.git,
                monorepo_url=self.settings.skills_monorepo_url or None,
                monorepo_branch=self.settings.skills_monorepo_branch or None,
            )
            # slots → используем object.__setattr__
            object.__setattr__(self, "_skills_repo", repo)
        return repo

    @property
    def scenarios_repo(self) -> GitScenarioRepository:
        repo = self._scenarios_repo
        if repo is None:
            repo = GitScenarioRepository(
                paths=self.paths,
                git=self.git,
                url=self.settings.scenarios_monorepo_url or None,
                branch=self.settings.scenarios_monorepo_branch or None,
            )
            object.__setattr__(self, "_scenarios_repo", repo)
        return repo

    @property
    def skill_ctx(self) -> SkillContextPort:
        port = self._skill_ctx_port
        if port is None:
            port = InprocSkillContext()
            object.__setattr__(self, "_skill_ctx_port", port)
        return port

    def reload_repos(self) -> None:
        object.__setattr__(self, "_skills_repo", None)
        object.__setattr__(self, "_scenarios_repo", None)

    def reload_repos(self) -> None:
        object.__setattr__(self, "_skills_repo", None)
        object.__setattr__(self, "_scenarios_repo", None)

    @property
    def i18n(self) -> I18nService:
        svc = self._i18n
        if svc is None:
            svc = I18nService(self)
            object.__setattr__(self, "_i18n", svc)
        return svc
