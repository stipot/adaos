from __future__ import annotations
from typing import Optional

from adaos.apps.bootstrap import get_ctx
from adaos.services.skill.context import SkillContextService
from adaos.ports.skill_context import CurrentSkill


def set_current_skill(name: str) -> bool:
    return SkillContextService(get_ctx()).set_current_skill(name)


def clear_current_skill() -> None:
    SkillContextService(get_ctx()).clear_current_skill()


def get_current_skill() -> Optional[CurrentSkill]:
    return SkillContextService(get_ctx()).get_current_skill()
