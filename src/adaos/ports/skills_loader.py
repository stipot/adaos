from __future__ import annotations
from pathlib import Path
from typing import Protocol


class SkillsLoaderPort(Protocol):
    async def import_all_handlers(self, skills_root: Path) -> None: ...
