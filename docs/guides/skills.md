# разработка навыков

1. подготовь каркас:

```bash
adaos skill create my_cool_skill
```

## Модель данных (SQLite)

* Таблица `skills`:

  * `name` (PK, уникальна)
  * `active_version` (опц.)
  * `repo_url` (опц.)
  * `installed` (`0/1`) — **источник истины** для «установлен/нет»
  * `last_updated` (timestamp)
* Таблица `skill_versions` (история версий/артефактов)

## Хранилище кода

* Путь: `{BASE_DIR}/skills` — **одно git-репо**.
* Выборка подпапок — через `git sparse-checkout`.
* Список подпапок = все `name` из `skills` с `installed=1`.

## Сервис

`services/skill/manager.py`:

* `list_installed()` — из БД (`installed=1`)
* `list_present()` — что реально есть в ФС
* `install(name)` — регистрирует в БД → пересобирает `sparse-set` → `git pull` → верификация
* `remove(name)` — `installed=0` → пересборка `sparse-set` → `git pull` → чистка каталога
* `sync()` — пересобирает `sparse-set` по БД и `git pull`

Все операции защищены Capabilities (`skills.manage`, `net.git`) и NetPolicy.

## CLI

```bash
adaos skill list            # из БД
adaos skill list --fs       # + сверка с ФС
adaos skill install <name>  # добавить в реестр и подтянуть в ФС
adaos skill remove <name>   # снять installed и пересобрать sparse
adaos skill sync            # приведение ФС к реестру
adaos skill reconcile-fs-to-db   # (опц.) отметить найденные папки installed=1
```
