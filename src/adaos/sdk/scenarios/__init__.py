# src/adaos/sdk/scenarios/__init__.py
from __future__ import annotations
from typing import Any, Dict, List, Optional
from pathlib import Path

from adaos.services.agent_context import get_ctx
from adaos.sdk.core.decorators import tool
from adaos.services.scenario.manager import ScenarioManager  # предпочтительно
from adaos.adapters.scenarios.git_repo import GitScenarioRepository
from adaos.adapters.db import SqliteScenarioRegistry


def _mgr():
    ctx = get_ctx()
    if ScenarioManager is not None:
        return ScenarioManager(
            git=ctx.git,
            paths=ctx.paths,
            caps=ctx.caps,
            settings=ctx.settings,
            registry=getattr(ctx, "scenario_registry", None),
            repo=getattr(ctx, "scenario_repo", None),
            bus=ctx.bus,
        )
    # fallback: собираем вручную (как в skills)
    if GitScenarioRepository is None or SqliteScenarioRegistry is None:
        raise RuntimeError("Scenario adapters are not available. Provide services.scenario.manager or adapters.")
    repo = GitScenarioRepository(
        paths=ctx.paths,
        git=ctx.git,
        monorepo_url=ctx.settings.scenarios_monorepo_url,
        monorepo_branch=ctx.settings.scenarios_monorepo_branch,
    )
    reg = SqliteScenarioRegistry(sql=ctx.sql)
    return type(
        "SimpleScenarioMgr",
        (),
        {
            "install": lambda self, sid: repo.install(sid),
            "uninstall": lambda self, sid: repo.uninstall(sid),
            "pull": lambda self, sid: repo.pull(sid),
            "push": lambda self, sid, message=None, signoff=False: repo.push(sid, message=message, signoff=signoff),
            "list_installed": lambda self: repo.list_installed(),
            "create": lambda self, sid, template="template": repo.create(sid, template=template),
            "delete": lambda self, sid: repo.delete(sid),
            "read_proto": lambda self, sid: repo.read_proto(sid),
            "write_proto": lambda self, sid, data: repo.write_proto(sid, data),
            "read_bindings": lambda self, sid, user: repo.read_bindings(sid, user),
            "write_bindings": lambda self, sid, user, data: repo.write_bindings(sid, user, data),
        },
    )()


# ---------- high-level SDK API (всё, что нужно LLM) ----------


@tool("scenarios.create", summary="create scenario from template", stability="experimental")
def create(sid: str, template: str = "template") -> str:
    """Создать сценарий из шаблона и зарегистрировать его."""
    p: Path = _mgr().create(sid, template=template)
    return str(p)


@tool("scenarios.install", summary="install (pull) scenario into runtime", stability="stable")
def install(sid: str) -> str:
    return _mgr().install(sid)


@tool("scenarios.uninstall", summary="uninstall scenario from runtime", stability="stable")
def uninstall(sid: str) -> str:
    return _mgr().uninstall(sid)


@tool("scenarios.pull", summary="pull/update scenario sources", stability="stable")
def pull(sid: str) -> str:
    return _mgr().pull(sid)


@tool("scenarios.push", summary="commit & push scenario changes", stability="experimental")
def push(sid: str, message: Optional[str] = None, signoff: bool = False) -> str:
    return _mgr().push(sid, message=message, signoff=signoff)


@tool("scenarios.list_installed", summary="list installed scenarios", stability="stable")
def list_installed() -> List[str]:
    m = _mgr()
    lst = getattr(m, "list_installed")()
    # поддержим оба варианта: список имён или объектов с .id/.name
    out: List[str] = []
    for it in lst or []:
        name = getattr(it, "id", None) or getattr(it, "name", None) or str(it)
        out.append(name)
    return out


@tool("scenarios.delete", summary="delete scenario locally", stability="experimental")
def delete(sid: str) -> bool:
    return _mgr().delete(sid)


# полезные утилиты для LLM/DevOps (безопасные IO)
@tool("scenarios.read_proto", summary="read scenario prototype JSON", stability="experimental")
def read_proto(sid: str) -> Dict[str, Any]:
    return _mgr().read_proto(sid)


@tool("scenarios.write_proto", summary="write scenario prototype JSON", stability="experimental")
def write_proto(sid: str, data: Dict[str, Any]) -> str:
    p: Path = _mgr().write_proto(sid, data)
    return str(p)


@tool("scenarios.read_bindings", summary="read scenario bindings for user", stability="experimental")
def read_bindings(sid: str, user: str) -> Dict[str, Any]:
    return _mgr().read_bindings(sid, user)


@tool("scenarios.write_bindings", summary="write scenario bindings for user", stability="experimental")
def write_bindings(sid: str, user: str, data: Dict[str, Any]) -> str:
    p: Path = _mgr().write_bindings(sid, user, data)
    return str(p)
