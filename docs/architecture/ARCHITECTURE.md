# AdaOS — Архитектура

## Слои

- **ports/** — протоколы (Git, FS, Secrets, Sandbox, Policy, KV/SQL, Repos).
- **adapters/** — реализации портов (git/sqlite/keyring/file-vault/fs/sandbox/audio/...).
- **services/** — бизнес-логика и политики (skills/scenarios managers, capabilities, net/fs policies, sandbox-service).
- **agent/** — runtime ядра (scenario_engine), платформенные слои.
- **integrations/** — внешние интеграции (ovos, rhasspy, inimatic, ...).
- **apps/** — bootstrap (сборка AgentContext) и CLI.
- **sdk/** — совместимость/dev-инструменты (тонкие обёртки).
- **config/** — константы/дефолты.
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
