## Базовые переменные

* `ADAOS_BASE_DIR` — база данных и артефактов (по умолчанию `~/.adaos`).
* `ADAOS_PROFILE` — профиль настроек (по умолчанию `default`).

## Константы монореп

`src/adaos/config/const.py`:

* `SKILLS_MONOREPO_URL`, `SKILLS_MONOREPO_BRANCH`
* `SCENARIOS_MONOREPO_URL`, `SCENARIOS_MONOREPO_BRANCH`
* `ALLOW_ENV_MONOREPO_OVERRIDE` — **False** (для dev/CI можно включить, чтобы брать URL из `.env`/ENV).

> Безопасность: URL **не** принимаются из CLI. Хосты автоматически добавляются в allow-list `NetPolicy`.

## Инициализация окружения

Колбэк CLI выполняет `prepare_environment()`:

* создаёт каталоги (`skills/`, `scenarios/`, `state/`, `cache/`, `logs/`);
* инициализирует схему БД (реестры и версии);
* при наличии URL монореп — выполняет начальное `ensure_repo` (без установки подпапок).
