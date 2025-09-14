# Декораторы

Модуль: `adaos.sdk.decorators`

## Подписки

```python
from adaos.sdk.decorators import subscribe

@subscribe("demo.hello")
async def handler(event):
    print("получено:", event)
````

Все подписки собираются в глобальном реестре `_SUBSCRIPTIONS`
и активируются при загрузке навыка.

## Инструменты

SDK поддерживает регистрацию "tools" (утилит) с публичными именами,
чтобы навыки могли объявлять API-интерфейсы для сценариев.
