# src\adaos\domain\skill.py
from __future__ import annotations
from dataclasses import dataclass
from adaos.domain import SkillId


@dataclass(frozen=True, slots=True)
class SkillMeta:
    id: SkillId
    name: str
    version: str
    path: str  # абсолютный путь на диске
