# SDK для LLM-навыков

AdaOS предоставляет тонкий SDK, который стабильно проксирует обращения
LLM-навыков к сервисам рантайма. Логика живёт в `services/*` и адаптерах,
а SDK лишь делегирует вызовы.

## Быстрый старт

```python
from adaos.sdk import memory
from adaos.sdk.i18n import _
```

Модули SDK можно импортировать в любое время. Однако обращаться к
рантайму (получать `AgentContext`) следует **только внутри функций** —
к моменту импорта контекст ещё не проинициализирован.

```python
from adaos.sdk.memory import get

def handler():
    # безопасно: require_ctx() вызывается внутри get()
    value = get("some-key", 0)
```

## Переводы до bootstrap

```python
from adaos.sdk.i18n import _

print(_("cli.help"))  # строка из пакетной локали доступна даже без контекста
```

SDK загружает локали из пакета (`adaos/locales/*.json`). После запуска
рантайма перевод делегируется в `I18nService`.

## Память навыка (KV)

```python
from adaos.sdk import memory

def on_call():
    counter = memory.get("runs", 0)
    memory.put("runs", counter + 1)
    return counter
```

Ключи автоматически неймспейсятся: `skills/<skill_id>/...` для активного
навыка и `global/...` когда текущий навык не выбран.

## Секреты

```python
from adaos.sdk import secrets

token = secrets.read("api-token")
secrets.write("api-token", "value")
```

Перед вызовом происходит проверка capability (`secrets.read` или
`secrets.write`). При отсутствии прав будет выброшено `CapabilityError`.

## Работа с временными файлами

```python
from adaos.sdk import fs

path = fs.save_bytes("run/output.bin", b"data")
with fs.open("run/output.bin", "rb") as fh:
    payload = fh.read()
```

Все пути ограничены sandbox-директорией `tmp/`; попытки выхода наружу
завершаются `ValueError`.

## Валидация навыка

```python
from adaos.sdk.manage import self_validate

report = self_validate(strict=True)
for issue in report.issues:
    print(issue.level, issue.message)
```

`self_validate()` делегирует вызов `SkillValidationService` и возвращает
`ValidationReport`.

## События и control-plane

```python
from adaos.sdk.events import publish

publish("skill.started", {"skill": "demo"})
```

Управляющие операции (`manage.scenario_toggle` и т.п.) также проверяют
capabilities и при отсутствии поддержки в рантайме выбрасывают
`NotImplementedError` с пояснением.

## Ошибки SDK

* `SdkRuntimeNotInitialized` — рантайм ещё не инициализирован (зовите
  `bootstrap_app()`/поднимайте контекст перед использованием).
* `CapabilityError` — навык пытается выполнить действие без нужных прав.
