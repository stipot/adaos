from __future__ import annotations
from dataclasses import dataclass, field
from typing import Optional
from time import time as _now


@dataclass(frozen=True, slots=True)
class SkillRecord:
    name: str
    # состояние установки в БД
    installed: bool = True
    # выбранная версия (если используешь skills/skill_versions)
    active_version: Optional[str] = None
    # исходный URL (если хранится)
    repo_url: Optional[str] = None
    # опционально: пин на коммит/тег (под будущие обновления)
    pin: Optional[str] = None
    # когда пометили installed=1
    installed_at: float = field(default_factory=lambda: _now())
    # от БД: last_updated (в секундах)
    last_updated: Optional[float] = None
