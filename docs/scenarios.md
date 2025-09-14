# Сценарии (Scenarios)

Сценарий (scenario) — это композиция навыков, объединённых в последовательность действий.  
Он определяет, **какие навыки** и **в каком порядке** должны срабатывать при определённых событиях.

---

## Что такое сценарий

- Представляет собой workflow поверх навыков.  
- Описан декларативно (YAML/JSON).  
- Выполняется движком сценариев (`scenario_engine`) внутри AgentRuntime.  

Пример использования:  

- сценарий «Каталогизация медиа» вызывает навыки: сканирование → индексирование → веб-доступ.  
- сценарий «Голосовой ассистент» объединяет навыки STT → Intent Recognition → TTS.  

---

## Структура

Файлы сценариев располагаются в `scenarios/`.

Пример:

```text
scenarios/
  └── catalog-media/
      ├── scenario.yaml
      └── tests/
````

### Пример `scenario.yaml`

```yaml
id: catalog-media
name: Catalog Media
version: 0.1.0
description: >
  Сканирует медиафайлы и делает их доступными через браузер.
steps:
  - skill: fs-scan
    action: scan
    args:
      path: ~/media
  - skill: indexer
    action: update_index
  - skill: web-server
    action: serve
    args:
      port: 8080
```

---

## Запуск сценария

CLI:

```bash
adaos scenarios run catalog-media
```

Runtime:

```python
from adaos.services.scenario import ScenarioManager

mgr = ScenarioManager()
mgr.run("catalog-media")
```

---

## Репозитории

- Как и навыки, сценарии могут храниться в монорепозитории (sparse-checkout)
  или в отдельных git-репозиториях.
- Источник истины: SQLite-реестр сценариев.

---

## Best practices

- Делайте шаги сценария атомарными (один action = одна операция).
- Используйте `id` навыков и сценариев, а не пути к файлам.
- Добавляйте автотесты для сценариев.
- Поддерживайте семантическую версию (`0.1.0`, `0.2.0` …).
- Документируйте сценарий (`description`) — это помогает при публикации.
