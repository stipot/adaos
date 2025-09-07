# Policy

## Capabilities

`services/policy/capabilities.py`

* Простая in-memory модель: `grant(subject, caps...)`, `require(subject, caps...)`.
* Поддержка префиксов (`"net.*"` валидирует `"net.git"`).
* В менеджерах навешаны проверки:

  * навыки: `"skills.manage"` (+ `"net.git"` на операции с сетью);
  * сценарии: `"scenarios.manage"` (+ `"net.git"`);
  * секреты: `"secrets.read"`, `"secrets.write"`.

## NetPolicy

`services/policy/net.py`

* Allow-list доменов. По умолчанию добавляются хосты из `*_MONOREPO_URL`.
* `SecureGitClient` проверяет:

  * `ensure_repo(url)` — URL в allow-list;
  * `pull(dir)` — `remote.origin.url` в allow-list.

## Process Sandbox

* Capability `"proc.run"` требуется для запуска внешних процессов через `SandboxService`.
* Лимиты и события шины контролируются сервисом; запуск вне `BASE_DIR` запрещён (проверка в `ProcSandbox`).

## FS-sandbox

`services/policy/fs.py` + `services/fs/safe_io.py`

* Разрешён доступ только внутри:

  * `{BASE_DIR}`, `skills/`, `scenarios/`, `state/`, `cache/`, `logs/`.
* Безопасные операции:

  * `ensure_dir(path, fs)`
  * `write_text_atomic(path, data, fs)`, `write_json_atomic(...)`
  * `remove_tree(path, fs)`
* Утилита `_safe_join` в моно-репозиториях предотвращает traversal.
