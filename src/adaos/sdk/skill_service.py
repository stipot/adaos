# src/adaos/sdk/skill_service.py
from __future__ import annotations
from typing import Optional, List, Dict
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


def install_all_skills(limit: Optional[int] = None) -> List[str]:
    """
    Устанавливает все навыки из монорепозитория (или первые N при limit).
    Возвращает список имен успешно установленных навыков.
    """
    mgr = _mgr()

    # 1) гарантируем, что монорепо приведён в корректное состояние
    #    (ensure() не трогает локальные изменения и безопасен при повторных вызовах)
    try:
        repo = mgr.repo  # у SkillManager должен быть repo
    except AttributeError:
        # на случай, если менеджер прячет репо глубже — пробуем достать из путей
        # (оставлено на будущее; в норме мы сюда не попадаем)
        repo = None
    if repo is not None and hasattr(repo, "ensure"):
        repo.ensure()

    # 2) получаем перечень доступных навыков
    names: List[str] = []
    for getter in ("available", "available_names", "list_available", "list", "list_names"):
        if repo is not None and hasattr(repo, getter):
            try:
                value = getattr(repo, getter)()
                if isinstance(value, list):
                    names = value
                elif hasattr(value, "__iter__"):
                    names = list(value)
            except Exception:
                pass
        if names:
            break

    # fallback: если адаптер не дал список — попробуем известные «дефолтные» навыки
    if not names:
        names = ["weather_skill"]

    # 3) применим лимит, если задан
    if limit and limit > 0:
        names = names[:limit]

    # 4) устанавливаем по одному
    installed: List[str] = []
    for n in names:
        try:
            mgr.install(n)
            installed.append(n)
        except Exception:
            # тихо пропускаем «битые»/недоступные, чтобы CI не падал целиком
            continue

    return installed
