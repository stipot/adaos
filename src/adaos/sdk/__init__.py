from __future__ import annotations
from typing import Optional, List
from adaos.apps.bootstrap import get_ctx  # или ваш init_ctx/get_ctx
from adaos.services.skill.manager import SkillManager


def _mgr() -> SkillManager:
    ctx = get_ctx()
    return SkillManager(
        ctx=ctx,
        repo=ctx.skills_repo,  # где вы его держите в контексте
        bus=ctx.bus,
        git=ctx.git,
        caps=ctx.caps,
        paths=ctx.paths,
        reg=ctx.skill_registry,
    )


def install(name: str) -> str:
    return _mgr().install(name)


def uninstall(name: str) -> str:
    return _mgr().uninstall(name)


def push(name: str, message: str, signoff: bool = False) -> str:
    return _mgr().push(name, message, signoff=signoff)


def pull(name: str) -> str:
    return _mgr().pull(name)


def install_all(limit: Optional[int] = None) -> List[str]:
    return _mgr().install_all(limit=limit)
