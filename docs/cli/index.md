# CLI AdaOS

AdaOS предоставляет интерфейс командной строки (CLI) на базе [Typer](https://typer.tiangolo.com/).  
Он используется для запуска API, управления навыками и сценариями, тестирования и администрирования.

---

## Установка

CLI устанавливается вместе с AdaOS:

```bash
pip install -e ".[dev]"
````

---

## Основные команды

* [api](api.md) — запуск HTTP API.
* [skills](skills.md) — управление навыками.
* [tests](tests.md) — запуск тестов.
* [misc](misc.md) — вспомогательные команды (`--help`, `--version`).

---

## Пример

```bash
# запустить API
adaos api serve --host 127.0.0.1 --port 8777

# создать навык из шаблона
adaos skills scaffold my-skill

# выполнить тесты
adaos tests run
```

````

---

### 📄 `docs/cli/api.md`
```markdown
# adaos api

Запуск HTTP API (FastAPI).

## Команды

### serve

```bash
adaos api serve --host 127.0.0.1 --port 8777 --reload --token <TOKEN>
````

Параметры:

* `--host` (по умолчанию `127.0.0.1`) — хост для API.
* `--port` (по умолчанию `8777`) — порт API.
* `--reload` — режим авто-перезапуска (для разработки).
* `--token` — X-AdaOS-Token (иначе берётся из переменной `ADAOS_TOKEN`).

API точка входа: `adaos.api.server:app`.

````

---

### 📄 `docs/cli/skills.md`
```markdown
# adaos skills

Управление навыками.

## Доступные подкоманды

- `scaffold NAME` — создать новый навык из шаблона.  
- `list` — показать список установленных навыков.  
- `install URL` — установить навык из git-репозитория.  
- `uninstall NAME` — удалить навык.  
- `sync` — синхронизировать репозитории.  

---

## Примеры

```bash
# scaffold
adaos skills scaffold hello-skill

# установка из git
adaos skills install https://github.com/example/skill-foo.git

# список установленных
adaos skills list
````

````

---

### 📄 `docs/cli/tests.md`
```markdown
# adaos tests

Запуск тестов в песочнице.

## Команда

```bash
adaos tests run
````

По умолчанию:

* создаётся временная директория (`ADAOS_BASE_DIR` в `Temp`),
* запускается pytest с профилем `tool`,
* используется песочница для изоляции.

---

## Опции

* `--venv` (будет опционально) — создание отдельного окружения для теста.

````

---

### 📄 `docs/cli/misc.md`
```markdown
# Прочие команды

## Справка

```bash
adaos --help
````

## Версия

```bash
adaos --version
```
