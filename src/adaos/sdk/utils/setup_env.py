# src/adaos/sdk/utils/setup_env.py
from __future__ import annotations
from pathlib import Path

from adaos.apps.bootstrap import get_ctx
from adaos.adapters.db import SqliteSkillRegistry
from adaos.adapters.skills.mono_repo import MonoSkillRepository


def prepare_environment() -> None:
    """
    Минимальная подготовка окружения для первого запуска:
      - создаёт каталоги (skills, scenarios, state, cache, logs)
      - инициализирует схему БД (skills/skill_versions и т.д.)
      - при наличии URL монорепо навыков — клонирует репозиторий (без установки навыков)
    """
    ctx = get_ctx()

    # каталоги
    dirs = [
        ctx.paths.skills_dir(),
        ctx.paths.scenarios_dir(),
        ctx.paths.state_dir(),
        ctx.paths.cache_dir(),
        ctx.paths.logs_dir(),
    ]
    for d in dirs:
        Path(d).mkdir(parents=True, exist_ok=True)

    # схема БД
    _ = SqliteSkillRegistry(ctx.sql)  # создаст таблицы, если их нет

    # (необязательно, но удобно) клонируем монорепо навыков один раз
    if ctx.settings.skills_monorepo_url:
        MonoSkillRepository(
            paths=ctx.paths,
            git=ctx.git,
            url=ctx.settings.skills_monorepo_url,
            branch=ctx.settings.skills_monorepo_branch,
        ).ensure()
