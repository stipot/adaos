from __future__ import annotations
from typing import Optional
from adaos.apps.bootstrap import get_ctx
from adaos.adapters.db import SqliteScenarioRegistry
from adaos.adapters.scenarios.mono_repo import MonoScenarioRepository
from adaos.services.scenario.manager import ScenarioManager


def _mgr() -> ScenarioManager:
    ctx = get_ctx()
    repo = MonoScenarioRepository(paths=ctx.paths, git=ctx.git, url=ctx.settings.scenarios_monorepo_url, branch=ctx.settings.scenarios_monorepo_branch)
    reg = SqliteScenarioRegistry(ctx.sql)
    return ScenarioManager(repo=repo, registry=reg, git=ctx.git, paths=ctx.paths, bus=ctx.bus, caps=ctx.caps, settings=ctx.settings)


def list_installed():
    return [r.name for r in _mgr().list_installed()]


def install_scenario(sid: str) -> str:
    meta = _mgr().install(sid)
    return f"Installed scenario: {meta.id.value} v{meta.version} @ {meta.path}"


def uninstall_scenario(sid: str) -> str:
    _mgr().remove(sid)
    return f"Uninstalled scenario: {sid}"


# совместимые алиасы
def pull_scenario(sid: str) -> str:
    return install_scenario(sid)


def update_from_repo(sid: str, ref: Optional[str] = None) -> str:
    return f"Update scenario: deferred (PR-later)."


def install_from_repo(repo_url: str, sid: Optional[str], ref: Optional[str], subpath: Optional[str]) -> str:
    return "Install from arbitrary repo is disabled in MVP (security)."


# файловые операции (prototype/impl/bindings) оставим как были, если они у тебя реализованы,
# либо временно заглушим:
def create_scenario(sid: str, template: str = "template"):
    raise NotImplementedError("Scenario templates are deferred.")


def read_prototype(sid: str):
    raise NotImplementedError("Deferred.")


def write_prototype(sid: str, data):
    raise NotImplementedError("Deferred.")


def read_impl(sid: str, user: str):
    raise NotImplementedError("Deferred.")


def write_impl(sid: str, user: str, data):
    raise NotImplementedError("Deferred.")


def read_bindings(sid: str, user: str):
    raise NotImplementedError("Deferred.")


def write_bindings(sid: str, user: str, data):
    raise NotImplementedError("Deferred.")


def push_scenario(sid: str, message: Optional[str] = None):
    msg = message or f"scenario: update {sid}"
    return _mgr().push(sid, msg, signoff=False)
