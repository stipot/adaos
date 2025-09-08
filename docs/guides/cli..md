# CLI Шпаргалка

```bash
adaos --help

# Навыки
adaos skill list [--fs]
adaos skill install <name>
adaos skill uninstall <name>
adaos skill sync
adaos skill push <name> -m "msg"
adaos skill reconcile-fs-to-db

# Сценарии
adaos scenario list [--fs]
adaos scenario install <sid>
adaos scenario uninstall <sid>
adaos scenario sync
adaos scenario push <sid> -m "msg"

# Секреты
adaos secret set/get/list/delete/export/import
adaos secret set <KEY> <VALUE> [--scope profile|global]
adaos secret get <KEY> [--show] [--scope ...]
adaos secret list [--scope ...]
adaos secret delete <KEY> [--scope ...]
adaos secret export [--show] [--scope ...]
adaos secret import <file.json> [--scope ...]

# Песочница
adaos sandbox profiles
adaos sandbox run "<cmd>" --profile handler --cwd ~/.adaos/skills/<skill> --inherit-env --env DEBUG=1
```

## Поведение по умолчанию

* CLI сам выполняет `prepare_environment()` (кроме `reset`) — создаёт каталоги и схему БД.
* Для сетевых операций (git) используется `SecureGitClient` и `NetPolicy`.
* Для файловых операций — `FSPolicy` + безопасные функции записи/удаления.

## Песочница

```bash
adaos sandbox profiles
adaos sandbox run "<команда>" \
  --profile handler \
  --cwd ~/.adaos/skills/weather_skill \
  --inherit-env \
  --env DEBUG=1 --env LOG_LEVEL=info
adaos sandbox run "<команда>" --profile handler --cwd \~/.adaos/skills/<skill> --inherit-env --env DEBUG=1
# Явные лимиты перекрывают профиль:
adaos sandbox run "python -c 'while True: pass'" --cpu 1 --wall 10

## Временно отложено (deferred)

* Генерация навыков из LLM, шаблоны, `push/rollback`, валидация/prep.
* Свободная установка из произвольных репозиториев.
