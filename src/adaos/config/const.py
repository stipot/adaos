# src/adaos/config/const.py
from __future__ import annotations

# ЖЁСТКИЕ значения по умолчанию (меняются разработчиками в коде/сборке)
SKILLS_MONOREPO_URL: str | None = "https://github.com/stipot/adaoskills.git"
SKILLS_MONOREPO_BRANCH: str | None = "main"

SCENARIOS_MONOREPO_URL: str | None = "https://github.com/stipot/adaosscens.git"
SCENARIOS_MONOREPO_BRANCH: str | None = "main"

# Разрешить ли .env/ENV менять монорепо (ТОЛЬКО для dev-сборок!)
ALLOW_ENV_MONOREPO_OVERRIDE: bool = False
