# src/adaos/sdk/skill_service.py
from __future__ import annotations
from typing import Optional, List
from adaos.apps.bootstrap import get_ctx


def _mgr():
    ctx = get_ctx()
    from adaos.services.skill.manager import SkillManager
    from adaos.adapters.db.sqlite_skill_registry import SqliteSkillRegistry
    from adaos.adapters.skills.mono_repo import MonoSkillRepository

    return SkillManager(
        paths=ctx.paths,
        git=ctx.git,
        caps=ctx.caps,
        registry=SqliteSkillRegistry(ctx.sql),
        repo=MonoSkillRepository(
            paths=ctx.paths,
            git=ctx.git,
            url=ctx.settings.skills_monorepo_url,
            branch=ctx.settings.skills_monorepo_branch,
        ),
        bus=getattr(ctx, "bus", None),  # если в контексте нет bus (тесты)
    )


def list_installed_skills() -> List[Dict[str, str]]:
    """
    Возвращает [{name, version}] только для installed==True.
    Без походов в git. Работает и в тестовом режиме.
    """
    ctx = get_ctx()
    from adaos.adapters.db.sqlite_skill_registry import SqliteSkillRegistry

    reg = SqliteSkillRegistry(ctx.sql)
    items = []
    for r in reg.list():
        if getattr(r, "installed", False):
            items.append(
                {
                    "name": r.name,
                    "version": r.active_version or "unknown",
                }
            )
    return items


def list_all_skills() -> List[Dict[str, str]]:
    """Пригодится на будущее: полный список из реестра."""
    ctx = get_ctx()
    from adaos.adapters.db.sqlite_skill_registry import SqliteSkillRegistry

    reg = SqliteSkillRegistry(ctx.sql)
    return [
        {
            "name": r.name,
            "version": r.active_version or "unknown",
            "installed": bool(getattr(r, "installed", False)),
        }
        for r in reg.list()
    ]


# Создание из шаблона (локально). push=False по умолчанию.
def create_skill(name: str, template: str = "demo_skill", register: bool = True, push: bool = False) -> str:
    from adaos.services.skill.scaffold import create_skill as _create

    return str(_create(name, template, register=register, push=push))


def install_skill(name: str) -> str:
    return _mgr().install(name)


def uninstall_skill(name: str) -> str:
    return _mgr().uninstall(name)


def pull_skill(name: str) -> str:
    return _mgr().pull(name)


def push_skill(name: str, message: str, signoff: bool = False) -> str:
    return _mgr().push(name, message, signoff=signoff)


def update_skill(name: str) -> str:
    return _mgr().update(name)


# Не обязательно для тестов; оставим «пустышкой» либо делегируй, если реализовано.
def install_all_skills(limit: Optional[int] = None) -> List[str]:
    return []
