from __future__ import annotations
import importlib.util
from pathlib import Path

from adaos.ports.skills_loader import SkillsLoaderPort


class ImportlibSkillsLoader(SkillsLoaderPort):
    async def import_all_handlers(self, skills_root: Path) -> None:
        for handler in skills_root.rglob("handlers/main.py"):
            spec = importlib.util.spec_from_file_location("handler", handler)
            module = importlib.util.module_from_spec(spec)  # noqa: F841
            assert spec and spec.loader
            spec.loader.exec_module(module)  # type: ignore[attr-defined]
