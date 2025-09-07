## Общая шпаргалка

```bash
adaos --help

# Skills
adaos skill list [--fs]
adaos skill install <name>
adaos skill remove <name>
adaos skill sync
adaos skill reconcile-fs-to-db

# Scenarios
adaos scenario list [--fs]
adaos scenario install <sid>
adaos scenario remove <sid>
adaos scenario sync

# Secrets
adaos secret set <KEY> <VALUE> [--scope profile|global]
adaos secret get <KEY> [--show] [--scope ...]
adaos secret list [--scope ...]
adaos secret delete <KEY> [--scope ...]
adaos secret export [--show] [--scope ...]
adaos secret import <file.json> [--scope ...]
```

## Поведение по умолчанию

* CLI сам выполняет `prepare_environment()` (кроме `reset`) — создаёт каталоги и схему БД.
* Для сетевых операций (git) используется `SecureGitClient` и `NetPolicy`.
* Для файловых операций — `FSPolicy` + безопасные функции записи/удаления.

## Временно отложено (deferred)

* Генерация навыков из LLM, шаблоны, `push/rollback`, валидация/prep.
* Свободная установка из произвольных репозиториев.
