"""Thin, import-safe facade for skill validation services."""

from __future__ import annotations

from typing import Optional

from adaos.services.skill.validation import SkillValidationService, ValidationReport

from adaos.sdk.core._ctx import require_ctx
from adaos.sdk.data.context import get_current_skill

__all__ = ["validate_self", "ValidationReport"]


def validate_self(strict: bool = False, probe_tools: bool = False) -> ValidationReport:
    ctx = require_ctx("sdk.validation.validate_self")
    current = get_current_skill()
    skill_id: Optional[str] = getattr(current, "name", None) if current else None
    return SkillValidationService(ctx).validate(skill_id, strict=strict, probe_tools=probe_tools)
