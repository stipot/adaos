# CONFIG

## ENV/CLI

- `ADAOS_BASE_DIR` — база данных/артефактов (`~/.adaos` по умолчанию)
- `ADAOS_PROFILE` — профиль (`default`)
- `ADAOS_GIT_NAME` / `ADAOS_GIT_EMAIL` — автор коммитов

## Константы монореп

`config/const.py`:

- `SKILLS_MONOREPO_URL`, `SKILLS_MONOREPO_BRANCH`
- `SCENARIOS_MONOREPO_URL`, `SCENARIOS_MONOREPO_BRANCH`
- `ALLOW_ENV_MONOREPO_OVERRIDE` (False)

## Prepare

`prepare_environment()`:

- создаёт каталоги (`skills/`, `scenarios/`, `state/`, `cache/`, `logs/`)
- инициализирует БД
- **клонирует** монорепо только если `.git` отсутствует (без pull/checkout)
