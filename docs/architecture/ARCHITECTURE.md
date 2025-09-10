# AdaOS — Архитектура

## Слои

domain → ports → services → adapters → apps

- **domain/** — SkillId, SkillMeta — единственный источник правды об идентичности/версии/пути навыка.
- **ports/** — протоколы (Git, FS, Secrets, Sandbox, Policy, KV/SQL, Repos).
  - SkillRepository: ensure/list/get/install/uninstall. Контракт один, но источник установки задаётся структурой InstallSpec. Сейчас сигнатура «url_or_name» смешивает режимы; починить через InstallSpec.
  - SkillRegistry: CRUD по SkillRecord (installed/active_version/repo_url/pin). Уже оформлено моделью.
  - SkillRuntime: start(skill, env)->RunHandle, stop(handle), status(handle).
  - SkillValidator: validate(skill)->ValidationReport. Внутри — статические и динамические проверки (как в текущем модуле).
  - SkillScaffolder: create(name, template)->Path.
  - SkillGenerator (LLM): generate(req)->GeneratedFiles (манифест + каркас handlers).
  - инфраструктурные: GitClient, PathProvider, EventBus (у вас уже используются).
- **services/** — бизнес-логика и политики (skills/scenarios managers, capabilities, net/fs policies, sandbox-service).
  - SkillManager: оркестрация сценариев «install/sync/uninstall/list/push/activate» через порты. Он уже есть — оставить как фасад.
  - SkillDevFlow: генерация → scaffold → validate → commit/push → register → PR (TBD)
- **adapters/** — реализации портов (git/sqlite/keyring/file-vault/fs/sandbox/audio/...).
  - repo: MonoSkillRepository (sparse в монорепо) и FsSkillRepository (one-repo-per-skill). Сейчас обе реализации есть, но читают манифест по-разному и по-разному трактуют install. Привести к одному контракту.
  - validator: тонкая оболочка вокруг текущего skill_validator.py. Он уже делает schema-check и безопасный импорт в отдельном процессе.
  - runtime: процесс-раннер, технически очень похож на динамическую часть валидатора (изолированный импорт handlers/main.py, регистрация декораторов, подписки и т. п.).
- **apps/** — (CLI/API/UI) bootstrap (сборка AgentContext).
  - CLI вызывает только сервисы; формирование текста (а не типов) — забота CLI. Сейчас местами CLI тянет адаптеры напрямую — переложить на SkillManager
- **integrations/** — внешние интеграции (ovos, rhasspy, inimatic, ...).
- **sdk/** — совместимость/dev-инструменты (тонкие обёртки).
- **config/** — константы/дефолты.
- **agent/** — runtime ядра (scenario_engine), платформенные слои.
- **utils/** — мелкие утилиты.

## AgentContext

Собирается в `apps/bootstrap.py`, включает:

- settings, paths, bus, proc
- caps (Capabilities), net (NetPolicy), fs (FSPolicy)
- sql + kv, secrets (Keyring/FileVault через SecretsService)
- git (SecureGitClient), sandbox (SandboxService)

### Документирование

```bash
pip install -r requirements-docs.txt
mkdocs build --strict
mkdocs serve
```

**Принцип:** сервисы получают **только нужные порты**, не «весь контекст».
