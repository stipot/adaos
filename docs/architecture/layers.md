# Слои архитектуры AdaOS

AdaOS реализует классическую слоистую модель:

```

domain → ports → services → adapters → apps

```

---

## Domain

`domain/` — базовые сущности и типы.  
Пример: `SkillId`, `SkillMeta` — единственный источник правды об идентичности, версии и пути навыка.

---

## Ports

`ports/` — протоколы интерфейсов. Определяют контракты, которые реализуются адаптерами:

- **SkillRepository**: `ensure / list / get / install / uninstall`  
  Контракт один, источник установки задаётся структурой `InstallSpec`.  
- **SkillRegistry**: CRUD по `SkillRecord` (installed, active_version, repo_url, pin).  
- **SkillRuntime**: `start(skill, env) -> RunHandle`, `stop(handle)`, `status(handle)`.  
- **SkillValidator**: `validate(skill) -> ValidationReport` (статические и динамические проверки).  
- **SkillScaffolder**: `create(name, template) -> Path`.  
- **SkillGenerator (LLM)**: `generate(req) -> GeneratedFiles` (манифест + каркас handlers).  

Инфраструктурные порты: `GitClient`, `PathProvider`, `EventBus`.

---

## Services

`services/` — бизнес-логика и политики.

- **SkillManager** — фасад оркестрации операций: install / sync / uninstall / list / push / activate.  
- **SkillDevFlow** — генерация → scaffold → validate → commit/push → register → PR (в разработке).  
- **SandboxService** — изоляция выполнения.  
- **Policies** — Capabilities, NetPolicy, FSPolicy.  

---

## Adapters

`adapters/` — реализации портов:

- **repo** — `MonoSkillRepository` (sparse-checkout в монорепо), `FsSkillRepository` (one-repo-per-skill).  
  > ⚠️ Сейчас они читают манифесты по-разному, требуется унификация.  
- **validator** — оболочка вокруг `skill_validator.py` (schema-check + безопасный импорт).  
- **runtime** — процесс-раннер (импорт `handlers/main.py`, регистрация декораторов, подписки).  
- **db** — SQLite-хранилища (registry, KV, store).  
- **secrets** — keyring + FileVault.  
- **git** — CLI Git и SecureGitClient.  

---

## Apps

`apps/` — CLI, API, UI.

- CLI вызывает только сервисы (формирование текста — забота CLI).  
- API — FastAPI, работает поверх тех же сервисов.  
- Bootstrap (`apps/bootstrap.py`) собирает `AgentContext`.  

> ⚠️ Важно: CLI не должно напрямую тянуть адаптеры. Всё взаимодействие должно идти через `SkillManager` и сервисы.

---

## Integrations

`integrations/` — внешние проекты (OVOS, Rhasspy, Inimatic и др.).

---

## SDK

`sdk/` — dev-инструменты и совместимость.  
Примеры: события (`adaos.sdk.bus`), декораторы (`subscribe`).

---

## Config и Utils

- **config/** — константы и дефолтные настройки.  
- **utils/** — мелкие утилиты.  

---

## Правило для MVP

- Вся бизнес-логика и состояние — в **services**.  
- Вся интеграция — в **adapters**.  
- CLI и тесты работают **поверх services**.  
- SDK — только тонкие фасады, без прямого доступа к хранилищам, git и секретам.  

```
