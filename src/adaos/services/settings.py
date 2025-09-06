from __future__ import annotations
from dataclasses import dataclass
import os


@dataclass(slots=True)
class Settings:
    base_dir: str
    profile: str = "default"

    @staticmethod
    def from_env() -> "Settings":
        base = os.environ.get("ADAOS_BASE_DIR") or os.path.expanduser("~/.adaos")
        profile = os.environ.get("ADAOS_PROFILE", "default")
        return Settings(base_dir=base, profile=profile)
