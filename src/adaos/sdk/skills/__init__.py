# src/adaos/sdk/skills/__init__.py
from __future__ import annotations
from typing import Optional, List

from adaos.services.agent_context import get_ctx
from adaos.services.skill.manager import SkillManager
from adaos.adapters.skills.git_repo import GitSkillRepository
from adaos.adapters.db.sqlite_skill_registry import SqliteSkillRegistry

# делегат в scaffold для create()
from adaos.services.skill.scaffold import create as _scaffold_create


def _mgr() -> SkillManager:
    ctx = get_ctx()
    repo = GitSkillRepository(paths=ctx.paths, git=ctx.git, monorepo_url=ctx.settings.skills_monorepo_url, monorepo_branch=ctx.settings.skills_monorepo_branch)
    reg = SqliteSkillRegistry(sql=ctx.sql)
    return SkillManager(
        git=ctx.git,
        paths=ctx.paths,
        caps=ctx.caps,
        settings=ctx.settings,
        registry=reg,
        repo=repo,
        bus=ctx.bus,
    )


# ---------- high-level SDK API ----------


def create(name: str, template: str = "demo_skill", *, register: bool = True, push: bool = True) -> str:
    """Создать навык из шаблона (вся логика resolve — внутри services.skill.scaffold)."""
    p = _scaffold_create(name, template=template, register=register, push=push)
    return str(p)


def install(name: str) -> str:
    return _mgr().install(name)


def uninstall(name: str) -> str:
    return _mgr().uninstall(name)


def pull(name: str) -> str:
    return _mgr().pull(name)


def push(name: str, message: str, signoff: bool = False) -> str:
    return _mgr().push(name, message, signoff=signoff)


def list_installed() -> List[str]:
    m = _mgr()
    return [r.name for r in m.list_installed() if getattr(r, "installed", True)]


def install_all(limit: Optional[int] = None) -> List[str]:
    """Лучшее усилие: ставим все доступные (или первую страницу), полезно для тестов/демо."""
    m = _mgr()
    try:
        m.repo.ensure()
    except Exception:
        pass

    names: List[str] = []
    try:
        for it in m.repo.list() or []:
            names.append(getattr(it, "name", it))
    except Exception:
        pass

    if not names:
        # запасной вариант для smoke-тестов
        names = ["weather_skill"]

    if limit and limit > 0:
        names = names[:limit]

    ok: List[str] = []
    for n in names:
        try:
            m.install(n)
            ok.append(n)
        except Exception:
            continue
    return ok
