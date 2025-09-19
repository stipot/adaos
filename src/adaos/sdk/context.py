from __future__ import annotations
from typing import Optional

from adaos.services.skill.context import SkillContextService
from adaos.ports.skill_context import CurrentSkill
from ._ctx import require_ctx


def set_current_skill(name: str) -> bool:
    ctx = require_ctx("Skill context requires runtime context")
    return SkillContextService(ctx).set_current_skill(name)


def clear_current_skill() -> None:
    ctx = require_ctx("Skill context requires runtime context")
    SkillContextService(ctx).clear_current_skill()


def get_current_skill() -> Optional[CurrentSkill]:
    ctx = require_ctx("Skill context requires runtime context")
    return SkillContextService(ctx).get_current_skill()
