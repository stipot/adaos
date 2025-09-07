# профили, окружение и события

Песочница процессов обеспечивает ограничение wall/cpu/memory и убивает всё дерево процесса
при превышении. В PR-12.1 добавлены профили лимитов, белый список переменных окружения и события.

## Профили лимитов

Профили описаны в `services/sandbox/profiles.py`:

- `default`:  wall=30s
- `prep`:     wall=60s, cpu=15s, rss=512MB
- `handler`:  wall=5s,  cpu=2s,  rss=256MB
- `tool`:     wall=15s, cpu=5s,  rss=512MB

Сервис `SandboxService` выбирает лимиты по приоритету:
`explicit limits` > `profile` > `default`.

## Окружение (env)

По умолчанию процессы запускаются **без наследования** окружения.
Можно включить безопасное наследование:

- разрешённые ключи: `PATH, HOME, LANG, LC_ALL, TMP, TEMP, TMPDIR` (POSIX),
  `Path, SystemRoot, USERNAME, USERPROFILE, APPDATA, LOCALAPPDATA, TEMP, TMP` (Windows);
- разрешённые префиксы: `ADAOS_*`, `PYTHON*`.

Дополнительные переменные можно передать аргументом `extra_env`, либо через CLI `--env KEY=VAL`.

## События в шину

`SandboxService` публикует:

- `sandbox.start` — `{cmd, cwd, profile, limits}`
- `sandbox.killed` — `{cmd, cwd, reason, duration}`
- `sandbox.end` — `{cmd, cwd, exit, timed_out, duration}`

Это позволяет строить аудит и метрики.

## Политики

Для запуска нужен capability `"proc.run"` (выдаётся ядру в bootstrap).
