from __future__ import annotations
import importlib.util
from pathlib import Path
from typing import Any

from adaos.ports.skills_loader import SkillsLoaderPort


class ImportlibSkillsLoader(SkillsLoaderPort):
    async def import_all_handlers(self, skills_root: Any) -> None:
        # допускаем Path | str | callable (например, paths.skills_dir)
        if callable(skills_root):
            skills_root = skills_root()
        root = Path(skills_root)
        for handler in root.rglob("handlers/main.py"):
            # уникализируем имя модуля по пути скилла, чтобы не затирать предыдущие
            mod_name = "adaos_skill_" + handler.parent.as_posix().replace("/", "_")
            spec = importlib.util.spec_from_file_location(mod_name, handler)
            module = importlib.util.module_from_spec(spec)  # noqa: F841
            assert spec and spec.loader
            spec.loader.exec_module(module)  # type: ignore[attr-defined]
