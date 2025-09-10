# src/adaos/services/agent_context.py
from __future__ import annotations
from dataclasses import dataclass, field
from typing import Any, Optional

from adaos.services.settings import Settings
from adaos.ports import EventBus, Process, Capabilities, Devices, KV, SQL, Secrets, Net, Updates, GitClient
from adaos.ports.paths import PathProvider
from adaos.ports.fs import FSPolicy
from adaos.ports.sandbox import Sandbox

from adaos.adapters.skills.git_repo import GitSkillRepository
from adaos.adapters.scenarios.git_repo import GitScenarioRepository


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

    # приватные кэши под slots
    _skills_repo: Optional[GitSkillRepository] = field(default=None, init=False, repr=False)
    _scenarios_repo: Optional[GitScenarioRepository] = field(default=None, init=False, repr=False)

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
                monorepo_url=self.settings.scenarios_monorepo_url or None,
                monorepo_branch=self.settings.scenarios_monorepo_branch or None,
            )
            object.__setattr__(self, "_scenarios_repo", repo)
        return repo

    def reload_repos(self) -> None:
        object.__setattr__(self, "_skills_repo", None)
        object.__setattr__(self, "_scenarios_repo", None)
