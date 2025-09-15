# Навыки (Skills)

Навык (skill) — это минимальная единица функциональности в AdaOS.  
Каждый навык оформлен как git-репозиторий с манифестом и кодом.

---

## Что такое skill

- Изолированный модуль с собственным `manifest.yaml`.  
- Реализует одну задачу: TTS, каталогизацию медиа, интеграцию с API.  
- Загружается в runtime и подписывается на события через SDK.  

Пример:  

```text
skills/
  └── hello-skill/
      ├── manifest.yaml
      └── handler.py
````

---

## Manifest

Файл `manifest.yaml` описывает идентичность и структуру навыка.

### Пример

```yaml
id: hello-skill
name: Hello World Skill
version: 0.1.0
entrypoint: handler.py
description: >
  Демонстрационный навык. Подписывается на события и отвечает "Hello".
dependencies:
  - requests
```

### Основные поля

- **id** — уникальный идентификатор навыка.
- **name** — человекочитаемое название.
- **version** — семантическая версия.
- **entrypoint** — точка входа (Python-файл с обработчиками).
- **description** — описание назначения.
- **dependencies** — список Python-зависимостей.

---

## Scaffold

CLI позволяет быстро создать навык из шаблона:

```bash
adaos skills scaffold hello-skill
```

Структура после scaffold:

```text
hello-skill/
  ├── manifest.yaml
  └── handler.py
```

`handler.py`:

```python
from adaos.sdk.decorators import subscribe

@subscribe("demo.hello")
async def on_hello(event):
    print("Hello from skill!", event)
```

---

## Использование навыка

```bash
adaos skill run weather_skill
adaos skill run weather_skill --topic nlp.intent.weather.get --payload '{"city": "Berlin"}'
```

## Репозитории

### Моно-репозиторий

- Все навыки хранятся в одном git working tree.
- Используется sparse-checkout (загружается только нужный навык).

### One-repo-per-skill

- Каждый навык — отдельный git-репозиторий.
- Удобно для независимой публикации.

AdaOS поддерживает оба варианта через `SkillRepository` (унифицированный контракт).

---

## Best practices

- Минимизируйте зависимости.
- Описывайте всё в `manifest.yaml`.
- Используйте `adaos.sdk.decorators.subscribe` вместо прямого доступа к шине.
- Секреты храните через `SecretsService`, а не в коде.
- Добавляйте автотесты (см. `adaos tests run`).
- Следуйте DevOps-циклу: scaffold → код → commit → тесты → sync.
