from __future__ import annotations
import os
from typing import Final
from adaos.ports.paths import PathProvider
from adaos.services.settings import Settings


class LocalPathProvider(PathProvider):
    def __init__(self, settings: Settings, ctx=None):
        self._s: Final[Settings] = settings
        self.ctx = ctx

    def _mk(self, *parts: str) -> str:
        p = os.path.join(*parts)
        os.makedirs(p, exist_ok=True)
        return p

    def base(self) -> str:
        return self._mk(self._s.base_dir)

    def skills_dir(self) -> str:
        return self._mk(self.base(), "skills")

    def scenarios_dir(self) -> str:
        return self._mk(self.base(), "scenarios")

    def state_dir(self) -> str:
        return self._mk(self.base(), "state")

    def cache_dir(self) -> str:
        return self._mk(self.base(), "cache")

    def logs_dir(self) -> str:
        return self._mk(self.base(), "logs")
