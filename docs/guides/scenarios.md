# Сценарии

## CLI

Работа со сценариями

```bash
# список сценариев
adaos scenario list

# создать из шаблона
adaos scenario create morning --template template

# установить из монорепо
adaos scenario install-repo --repo https://github.com/stipot/adaosscens.git --sid morning --ref main

# обновить из источника
adaos scenario update morning --ref main

# отметить установленным (алиас pull)
adaos scenario install morning
adaos scenario pull morning

# пуш изменений
adaos scenario push morning -m "Tune morning flow"

# показать прототип
adaos scenario show morning

# эффективная модель (с учётом I)
adaos scenario effective morning --user alice

# имплементация
adaos scenario impl get morning --user alice
adaos scenario impl set morning --user alice --data ./impl.alice.json

# биндинги
adaos scenario bindings get morning --user alice
adaos scenario bindings set morning --user alice --data '{"slots":{"weather":{"skill":"open_weather"}}}'

# запуск/инстансы/стоп
adaos scenario run morning --user alice --io '{"settings":{"output.active":"voice"}}'
adaos scenario instances
adaos scenario stop <iid>
adaos scenario stop-by-activity video:livingroom
```

## Модель данных (SQLite)

* Таблица `scenarios`:

  * `name` (PK), `active_version`, `repo_url`, `installed`, `last_updated`
* Таблица `scenario_versions` — как у навыков.

## Хранилище кода

* Путь: `{BASE_DIR}/scenarios` — **одно git-репо** (моно-репо сценариев).
* Выборка подпапок через `git sparse-checkout` по БД.

## Сервис и CLI

Сервис: `services/scenario/manager.py` (аналогично `SkillManager`).

CLI:

```bash
adaos scenario list
adaos scenario list --fs
adaos scenario install <sid>
adaos scenario remove <sid>
adaos scenario sync
```
