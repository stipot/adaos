# src/adaos/sdk/validation/skill.py
from __future__ import annotations
from typing import Optional
from adaos.services.skill.validation import SkillValidationService
from adaos.services.agent_context import get_ctx  # если есть такой хелпер

# реэкспорт типов отчёта
from adaos.sdk.validation.skill import ValidationReport as ValidationReport  # если тип у тебя уже здесь — оставь


def validate_skill(skill_name: Optional[str] = None, install_mode: bool = False, probe_tools: bool = False):
    # тонкая прослойка: вызов services
    ctx = get_ctx()
    return SkillValidationService(ctx).validate(skill_name, strict=install_mode, probe_tools=probe_tools)
