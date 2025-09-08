# Навыки (Skills)

## Хранилище и реестр

- `{BASE_DIR}/skills` — одно git-репо (моно).
- SQLite-таблица `skills` (`installed=1` = установлен), `skill_versions` — версии.

## Операции

- `install(name)` — добавить в реестр → sparse-set → pull → верификация.
- `uninstall(name)` — снять installed → sparse-set → pull → чистка каталога.
- `sync()` — пересобрать sparse по БД.
- `push(name, msg)` — коммит **только подпапки** навыка → push.

Все операции защищены Capabilities (`skills.manage`, `git.write`, `net.git`) и NetPolicy.

## CLI

```bash
adaos skill list [--fs]
adaos skill install <name>
adaos skill uninstall <name>
adaos skill sync
adaos skill create my_cool_skill
adaos skill push <name> -m "message"
```

## Безопасность

- Git URL монорепо берётся из констант/ENV (не из CLI).
- `ensure_clean(...)` блокирует `install/uninstall/sync`, если есть незакоммиченные правки под управляемыми путями.
- Deny-лист при push: `*.pem, *.key, *.pfx, *.p12, *vault.json, .env*, secrets*.json, *_secrets.json`.

# разработка навыков

1. подготовь каркас:

```bash
adaos skill create my_cool_skill
```

## Модель данных (SQLite)

- Таблица `skills`:

  - `name` (PK, уникальна)
  - `active_version` (опц.)
  - `repo_url` (опц.)
  - `installed` (`0/1`) — **источник истины** для «установлен/нет»
  - `last_updated` (timestamp)
- Таблица `skill_versions` (история версий/артефактов)

## Хранилище кода

- Путь: `{BASE_DIR}/skills` — **одно git-репо**.
- Выборка подпапок — через `git sparse-checkout`.
- Список подпапок = все `name` из `skills` с `installed=1`.

## Сервис

`services/skill/manager.py`:

- `list_installed()` — из БД (`installed=1`)
- `list_present()` — что реально есть в ФС
- `install(name)` — регистрирует в БД → пересобирает `sparse-set` → `git pull` → верификация
- `uninstall(name)` — `installed=0` → пересборка `sparse-set` → `git pull` → чистка каталога
- `sync()` — пересобирает `sparse-set` по БД и `git pull`

Все операции защищены Capabilities (`skills.manage`, `net.git`) и NetPolicy.
