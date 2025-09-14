# src/adaos/services/settings.py
from __future__ import annotations
from dataclasses import dataclass, replace
import os
from pathlib import Path
from typing import Optional, Dict
from adaos.config import const


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
    package_dir = Path(__file__).resolve().parent.parent
    env_type = os.getenv("ENV_TYPE", "prod")
    override_base_suffix = os.getenv("ADAOS_BASE_DIR_SUFFIX", "")
    override_base = os.getenv("ADAOS_BASE_DIR")
    base_dir: Path
    profile: str = "default"
    git_author_name: str = "AdaOS Bot"
    git_author_email: str = "bot@adaos.local"
    default_wall_time_sec: float = 30.0
    default_cpu_time_sec: float | None = None
    default_max_rss_mb: int | None = None

    # жёсткие (или dev-override через .env)
    skills_monorepo_url: Optional[str] = const.SKILLS_MONOREPO_URL
    skills_monorepo_branch: Optional[str] = const.SKILLS_MONOREPO_BRANCH
    scenarios_monorepo_url: Optional[str] = const.SCENARIOS_MONOREPO_URL
    scenarios_monorepo_branch: Optional[str] = const.SCENARIOS_MONOREPO_BRANCH

    @staticmethod
    def from_sources(env_file: Optional[str] = ".env") -> "Settings":
        def is_android() -> bool:
            return "ANDROID_BOOTLOGO" in os.environ or os.getenv("KIVY_BUILD", "") == "android"

        def _android_base_dir() -> Path:
            try:
                from jnius import autoclass

                PythonActivity = autoclass("org.kivy.android.PythonActivity")
                context = PythonActivity.mActivity
                ext = context.getExternalFilesDir(None)
                if ext is not None:
                    return Path(ext.getAbsolutePath()) / ".adaos"
                return Path(context.getFilesDir().getAbsolutePath()) / ".adaos"
            except Exception:
                return Path("./.adaos").resolve()

        def _get_base_dir() -> Path:
            env_type = os.getenv("ENV_TYPE", "prod")
            override_base_suffix = os.getenv("ADAOS_BASE_DIR_SUFFIX", "")
            package_dir = Path(__file__).resolve().parent.parent
            override_base = os.getenv("ADAOS_BASE_DIR")
            if override_base:
                return Path(override_base).expanduser().resolve()
            if is_android():
                return _android_base_dir()
            if env_type == "dev":
                return package_dir.parent.parent / f".adaos{override_base_suffix}"
            return Path(os.getenv("BASE_DIR") or (Path.home() / ".adaos")).resolve()

        env_file_vars = _parse_env_file(env_file) if env_file else {}

        def pick_env(key: str, default: Optional[str] = None) -> str:
            return os.environ.get(key) or env_file_vars.get(key) or (default or "")

        base = _get_base_dir()
        profile = pick_env("ADAOS_PROFILE", "default")

        # монорепо — ТОЛЬКО из констант, а .env/ENV учитываем если явно разрешено dev-флагом
        skills_url = const.SKILLS_MONOREPO_URL
        skills_branch = const.SKILLS_MONOREPO_BRANCH
        scenarios_url = const.SCENARIOS_MONOREPO_URL
        scenarios_branch = const.SCENARIOS_MONOREPO_BRANCH

        allow_override = const.ALLOW_ENV_MONOREPO_OVERRIDE or pick_env("ADAOS_ALLOW_UNSAFE_MONOREPO", "0") == "1"
        if allow_override:
            skills_url = pick_env("ADAOS_SKILLS_MONOREPO_URL", skills_url) or skills_url
            skills_branch = pick_env("ADAOS_SKILLS_MONOREPO_BRANCH", skills_branch) or skills_branch
            scenarios_url = pick_env("ADAOS_SCENARIOS_MONOREPO_URL", scenarios_url) or scenarios_url
            scenarios_branch = pick_env("ADAOS_SCENARIOS_MONOREPO_BRANCH", scenarios_branch) or scenarios_branch

        return Settings(
            base_dir=base,
            profile=profile,
            skills_monorepo_url=skills_url,
            skills_monorepo_branch=skills_branch,
            scenarios_monorepo_url=scenarios_url,
            scenarios_monorepo_branch=scenarios_branch,
        )

    def with_overrides(self, **kw) -> "Settings":
        # перегружать можно ТОЛЬКО безопасные поля (base_dir/profile)
        safe = {k: v for k, v in kw.items() if k in {"base_dir", "profile"} and v is not None}
        return replace(self, **safe)
