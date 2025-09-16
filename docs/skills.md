# Навыки (Skills)

Навык (skill) — это минимальная единица функциональности в AdaOS.  
Каждый навык оформлен как git-репозиторий с манифестом и кодом.

---

## Что такое skill

- Изолированный модуль с собственным `skill.yaml`.  
- Реализует одну задачу: получения данных о погоде, каталогизацию медиа, интеграцию с API.  
- Использует SDK для доступа к ядру AdaOS

Структура папок:  

```text
skills/
  └── skill-name/
      ├── handlers
        └── main.py         # start script
      ├── i18n
        └── ru.json         # locales data
        └── en.json
      ├── prep
        └── prep_prompt.md  # preparation request to LLM
        └── prepare.py      # preparation code
      ├── tests
        └── conftest.py     # tests
      ├── .skill_env.json   # skill env data
      ├── config.json       # skill conf data
      ├── prep_result.json  # preparation result
      ├── skill_prompt.md   # skill generation request to LLM
      └── skill.yaml        # Skill metadata
````

---

## Manifest

Файл `manifest.yaml` описывает идентичность и структуру навыка.

### Пример

```yaml
name: weather_skill
version: 1.0.4
description: Навык для получения текущей погоды на сегодня
runtime:
  python: "3.11"
dependencies:
  - requests>=2.31
events:
  subscribe:
    - "nlp.intent.weather.get"
  publish:
    - "ui.notify"
tools:
  - name: "get_weather"
    input_schema:
      type: object
      required: [city]
      properties:
        city: { type: string, minLength: 1 }
    output_schema:
      type: object
      required: [ok]
      properties:
        ok: { type: boolean }
        city: { type: string }
        temp: { type: number }
        description: { type: string }
```

### Основные поля

- **id** — уникальный идентификатор навыка.
- **name** — человекочитаемое название.
- **version** — семантическая версия.
- **description** — описание назначения.
- **dependencies** — список Python-зависимостей.
- **events** — подписки
- **tools** — методы: название, входные и выходные схемы

---

## Pipeline

```bash
adaos skill create skill-name -t demo_skill # Template is optional
# подготовить запрос на исследовательский код
adaos llm build-prep skill-name "Научись отправлять уведомления в Telegram через Bot API при получении события"
# подготовить запрос на создание навыка
adaos llm build-skill skill-name "Научись отправлять уведомления в Telegram через Bot API при получении события"

```

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
