from __future__ import annotations
from typing import Optional
from adaos.apps.bootstrap import get_ctx
from adaos.adapters.db import SqliteSkillRegistry
from adaos.adapters.skills.mono_repo import MonoSkillRepository
from adaos.services.skill.manager import SkillManager


def _mgr() -> SkillManager:
    ctx = get_ctx()
    repo = MonoSkillRepository(paths=ctx.paths, git=ctx.git, url=ctx.settings.skills_monorepo_url, branch=ctx.settings.skills_monorepo_branch)
    reg = SqliteSkillRegistry(ctx.sql)
    return SkillManager(repo=repo, registry=reg, git=ctx.git, paths=ctx.paths, bus=ctx.bus, caps=ctx.caps)


# ---- базовые операции, используются в CLI ----


def install_skill(skill_name: str) -> str:
    meta = _mgr().install(skill_name)
    return f"Installed: {meta.id.value} v{meta.version} @ {meta.path}"


def uninstall_skill(skill_name: str) -> str:
    _mgr().remove(skill_name)
    return f"Uninstalled: {skill_name}"


def pull_skill(skill_name: str) -> str:
    # в mono это то же, что ensure + sparse_set + pull (через install)
    return install_skill(skill_name)


def update_skill() -> str:
    # старый код обновлял «текущий» навык; в MVP просто no-op
    return "Update: not implemented in MVP (deferred)."


def install_skill_dependencies(skill_path) -> bool:
    # заглушка на MVP: зависимости не устанавливаем
    return True


def install_all_skills(limit: Optional[int] = None):
    # по продукту мы НЕ устанавливаем все публичные навыки — оставим пустой список
    return []


# ---- Dev-операции — отложены ----


def create_skill(skill_name: str, template: str) -> str:
    raise NotImplementedError("Skill creation via templates is deferred (PR-later).")


def push_skill(skill_name: str, message: str) -> str:
    raise NotImplementedError("Push to monorepo is deferred (PR-later).")


def update_skill_version(skill_name: str, version: str, path: str, status: str = "available") -> None:
    # совместимость, если где-то вызывалось
    from adaos.adapters.db.sqlite import update_skill_version as _upd

    _upd("skills", skill_name, version, path, status)


def rollback_last_commit() -> None:
    raise NotImplementedError("Rollback is deferred (PR-later).")
