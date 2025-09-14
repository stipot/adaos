# Событийная шина (bus)

Модуль: `adaos.sdk.bus`

## Основные функции

```python
from adaos.sdk import bus

await bus.emit("topic", {"key": "value"})
await bus.on("topic", handler)
````

* \*\*emit(topic, payload, **kw)** — публикует событие.
* **on(topic, handler)** — подписка на события.

## Пример

```python
from adaos.sdk.bus import emit, on

async def on_boot(event):
    print("boot event:", event)

await on("sys.boot.start", on_boot)
await emit("demo.hello", {"msg": "world"})
```
