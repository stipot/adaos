from __future__ import annotations
from dataclasses import dataclass, replace
import os
from pathlib import Path
from typing import Optional, Dict


def _parse_env_file(path: str) -> Dict[str, str]:
    data: Dict[str, str] = {}
    p = Path(path)
    if not p.exists():
        return data
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" in line:
            k, v = line.split("=", 1)
            data[k.strip()] = v.strip().strip('"').strip("'")
    return data


@dataclass(frozen=True, slots=True)
class Settings:
    base_dir: str
    profile: str = "default"

    @staticmethod
    def from_sources(
        cli: Optional[Dict[str, Optional[str]]] = None,
        env_file: Optional[str] = ".env",
    ) -> "Settings":
        # 1) .env (низкий приоритет), 2) ENV, 3) CLI (высший приоритет)
        env_file_vars = _parse_env_file(env_file) if env_file else {}

        def pick(key: str, default: Optional[str] = None) -> str:
            return (cli or {}).get(key) or os.environ.get(key) or env_file_vars.get(key) or default or ""

        base = pick("ADAOS_BASE_DIR", os.path.expanduser("~/.adaos"))
        profile = pick("ADAOS_PROFILE", "default")
        return Settings(base_dir=base, profile=profile)

    def with_overrides(self, **kw) -> "Settings":
        # иммутабельная «перегрузка» значений
        return replace(self, **{k: v for k, v in kw.items() if v is not None})
