# src/adaos/sdk/validation/skill.py
from __future__ import annotations

from typing import Optional

from adaos.services.skill.validation import SkillValidationService, ValidationReport

from .._runtime import require_ctx
from ..context import get_current_skill

__all__ = ["validate_self", "ValidationReport"]


def validate_self(strict: bool = False, probe_tools: bool = False) -> ValidationReport:
    ctx = require_ctx("sdk.validation.validate_self")
    current = get_current_skill()
    skill_id: Optional[str] = getattr(current, "name", None) if current else None
    return SkillValidationService(ctx).validate(skill_id, strict=strict, probe_tools=probe_tools)
