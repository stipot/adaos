# src/adaos/adapters/fs/path_provider.py
from __future__ import annotations
from dataclasses import dataclass
from pathlib import Path
from typing import Optional
from adaos.services.settings import Settings


@dataclass(slots=True)
class PathProvider:
    """Единая точка истинны для путей. Всегда работает с pathlib.Path."""

    base: Path
    package_dir: Path

    # --- конструкторы ---
    @classmethod
    def from_settings(cls, settings: Settings) -> "PathProvider":
        return cls(base=Path(settings.base_dir).expanduser().resolve())

    # совместимость со старым стилем: PathProvider(settings)
    def __init__(self, settings: Settings | str | Path):
        base = settings.base_dir
        package_dir = settings.package_dir
        object.__setattr__(self, "base", base.expanduser().resolve())
        object.__setattr__(self, "package_dir", package_dir.expanduser().resolve())

    # --- базовые каталоги ---
    def locales_dir(self) -> Path:  # TODO Move to global context&
        return (self.package_dir / "locales").resolve()

    def base_dir(self) -> Path:
        return self.base

    def skills_dir(self) -> Path:
        return (self.base / "skills").resolve()

    def scenarios_dir(self) -> Path:
        return (self.base / "scenarios").resolve()

    def skill_templates_dir(self) -> Path:
        return (self.base / "skills_templates").resolve()

    def scenario_templates_dir(self) -> Path:
        return (self.base / "scenario_templates").resolve()

    def models_dir(self) -> Path:
        return (self.base / "models").resolve()

    def logs_dir(self) -> Path:
        return (self.base / "logs").resolve()

    def cache_dir(self) -> Path:
        return (self.base / "cache").resolve()

    def state_dir(self) -> Path:
        return (self.base / "state").resolve()

    def tmp_dir(self) -> Path:
        return (self.base / "tmp").resolve()

    def ensure_tree(self) -> None:
        for p in (
            self.locales_dir(),
            self.base_dir(),
            self.skills_dir(),
            self.scenarios_dir(),
            self.skill_templates_dir(),
            self.scenario_templates_dir(),
            self.models_dir(),
            self.logs_dir(),
            self.cache_dir(),
            self.state_dir(),
            self.tmp_dir(),
        ):
            p.mkdir(parents=True, exist_ok=True)
