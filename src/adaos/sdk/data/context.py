from __future__ import annotations
from typing import Optional

from adaos.services.skill.context import SkillContextService
from adaos.ports.skill_context import CurrentSkill

from adaos.sdk.core._ctx import require_ctx


def _service() -> SkillContextService:
    ctx = require_ctx("sdk.data.context")
    return SkillContextService(ctx)


def set_current_skill(name: str) -> bool:
    return _service().set_current_skill(name)


def clear_current_skill() -> None:
    _service().clear_current_skill()


def get_current_skill() -> Optional[CurrentSkill]:
    return _service().get_current_skill()
