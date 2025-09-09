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

    # --- конструкторы ---
    @classmethod
    def from_settings(cls, settings: Settings) -> "PathProvider":
        return cls(base=Path(settings.base_dir).expanduser().resolve())

    # совместимость со старым стилем: PathProvider(settings)
    def __init__(self, settings_or_base: Settings | str | Path):
        if isinstance(settings_or_base, Settings):
            base = Path(settings_or_base.base_dir)
        else:
            base = Path(settings_or_base)
        object.__setattr__(self, "base", base.expanduser().resolve())

    # --- базовые каталоги ---
    def base_dir(self) -> Path:
        return self.base

    def skills_dir(self) -> Path:
        return (self.base / "skills").resolve()

    def scenarios_dir(self) -> Path:
        return (self.base / "scenarios").resolve()

    def logs_dir(self) -> Path:
        return (self.base / "logs").resolve()

    def cache_dir(self) -> Path:
        return (self.base / "cache").resolve()

    def state_dir(self) -> Path:
        return (self.base / "state").resolve()

    def tmp_dir(self) -> Path:
        return (self.base / "tmp").resolve()

    # --- полезно ---
    def ensure_tree(self) -> None:
        for p in (self.base_dir(), self.skills_dir(), self.scenarios_dir(), self.logs_dir(), self.cache_dir(), self.state_dir(), self.tmp_dir()):
            p.mkdir(parents=True, exist_ok=True)
