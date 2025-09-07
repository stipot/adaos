# src/adaos/sdk/skill_service.py
from __future__ import annotations
from typing import Optional, List
from adaos.apps.bootstrap import get_ctx


# --- Менеджер навыков и реестр ---
def _mgr():
    ctx = get_ctx()
    # импорт локально, чтобы не было циклов
    from adaos.services.skill.manager import SkillManager
    from adaos.adapters.db.sqlite_skill_registry import SqliteSkillRegistry

    return SkillManager(paths=ctx.paths, reg=SqliteSkillRegistry(ctx.sql), git=ctx.git, caps=ctx.caps)


# ---- CRUD / DevOps ----
def create_skill(name: str, template: str = "demo_skill", register: bool = True, push: bool = False) -> str:
    from adaos.services.skill.scaffold import create_skill as _create

    return str(_create(name, template, register=register, push=push))


def install_skill(name: str) -> str:
    # НИКАКОЙ пост-обработки и доступа к .id — просто проксируем ответ менеджера
    return _mgr().install(name)


def uninstall_skill(name: str) -> str:
    return _mgr().uninstall(name)


def pull_skill(name: str) -> str:
    return _mgr().pull(name)


def push_skill(name: str, message: str, signoff: bool = False) -> str:
    return _mgr().push(name, message, signoff=signoff)


def update_skill(name: str) -> str:
    return _mgr().update(name)


# (опционально) «всё» — можно заглушить в тестах или аккуратно делегировать, если есть реализация:
def install_all_skills(limit: Optional[int] = None) -> List[str]:
    # для тестов не используется; если нужно — делегируй в менеджер/репозиторий
    return []
