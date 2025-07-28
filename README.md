# AdaOS

## Структура директорий локальной версии AdaOS (MVP)
adaos/
├── runtime/                     # Skill Runtime
│   ├── skills/                  # Папки навыков
│   │   ├── AlarmSkill/
│   │   │   ├── active/          # Активная версия навыка
│   │   │   ├── staging/         # Скачанная версия перед активацией
│   │   │   └── rollback/        # Последняя рабочая версия
│   ├── tests/                   # YAML-тесты
│   ├── runtime.log              # Логи работы
│   ├── version_history.json     # Журнал локальных версий
│   └── runtime.py               # Запуск Runtime
├── skill_db.sqlite               # SQLite-хранилище метаданных
├── skills_repo/                  # Git-репозиторий навыков
│   └── .git/
├── llm_client.py                  # Интеграция с LLM
├── test_runner.py                 # TestRunner
├── process_llm_output.py          # Постобработка навыков
├── git_utils.py                   # Работа с Git
└── cli.py                         # CLI-интерфейс управления (Typer)

## **Схема локальной версии (PlantUML)**

```plantuml
@startuml Local_AdaOS_MVP
!includeurl https://raw.githubusercontent.com/plantuml-stdlib/C4-PlantUML/master/C4_Container.puml

Container(user, "Пользователь", "Голос или текст", "Формулирует запросы")
Container(llm, "LLM Client", "OpenAI / litellm", "Генерация тестов и навыков по запросу")
Container(testrunner, "Test Runner", "PyYAML + pytest", "Прогоняет тесты BDD для навыков")
Container(git, "Git Repo", "GitPython", "Версионирование навыков")
Container(sqlite, "Skill DB", "SQLite + SQLAlchemy", "Метаданные навыков и версий")
Container(runtime, "Skill Runtime", "importlib + watchdog", "Запуск навыков и проверка прав")
Container(logs, "Логирование", "logging + rich", "Хранение логов и ошибок")

Rel(user, llm, "Говорит/пишет запрос")
Rel(llm, testrunner, "Создает тест")
Rel(testrunner, runtime, "Проверяет в песочнице")
Rel(testrunner, git, "Сохраняет успешный навык")
Rel(testrunner, sqlite, "Записывает метаданные")
Rel(runtime, sqlite, "Читает активные версии навыков")
Rel(runtime, logs, "Пишет логи работы и ошибок")

@enduml
```

## **Как работают библиотеки и сервисы**

1. **LLM** (OpenAI / Ollama через `openai` или `litellm`)

   * Генерирует тест (YAML) и код навыка.

2. **TestRunner** (`PyYAML`, `pytest`)

   * Прогоняет тест на существующих навыках.
   * Если провал – генерирует и тестирует новый навык в песочнице.

3. **GitPython**

   * Хранит каждую версию навыка в `skills_repo/`.
   * Теги: `AlarmSkill_v1.0`.

4. **SQLite + SQLAlchemy**

   * Записывает: версия, путь к активной директории навыка, дата создания.

5. **Runtime** (`importlib`, `watchdog`)

   * Подхватывает активные навыки из `skills/active/`.
   * Обрабатывает intent → вызывает handler.py → проверяет права.

6. **Логирование** (`logging + rich`)

   * Все ошибки тестов и Runtime пишутся в `runtime.log`.
   * Возможна интеграция с CLI для просмотра.

## Skill

### **1. Основной принцип: навык = код + манифест (SDK)**

* **Каждый навык — это модуль на Python**: `manifest.yaml + handler.py`.
* **SDK максимально минималистичный** (10–15 функций) и похож на известные фреймворки (Flask, FastAPI, Alexa Skills Kit).
* Навык выполняется в общем рантайме без тяжёлого sandbox, но с **системой прав как в Android**.

Пример:

```yaml
# manifest.yaml
name: AlarmSkill
version: 1.0
permissions:
  - audio.playback
  - time.schedule
intents:
  - set_alarm
  - cancel_alarm
```

```python
# handler.py
from skill_sdk import speak, set_alarm, cancel_alarm

def handle(intent, entities):
    if intent == "set_alarm":
        set_alarm(entities["time"])
        speak("Будильник установлен")
    else:
        cancel_alarm()
        speak("Будильник отменён")
```

### **2. Система прав вместо глубокой изоляции**

* Навык при установке получает **фиксированный набор прав** на ресурсы (микрофон, TTS, сетевые вызовы и т.д.).
* Магазин проверяет права и код (статический анализ).
* В случае критической ошибки возможен быстрый откат навыка через CI/CD.


### **3. Генерация навыков через LLM и визуальный UI**

* **LLM выступает главным создателем навыков**, получая только компактную документацию по SDK и несколько примеров.
* Для пользователей без навыков программирования на сервере AdaOS делаем **UI-конструктор**:

  * Пользователь описывает навык голосом или текстом.
  * LLM генерирует код и манифест.
  * Код проверяется и устанавливается через магазин.

Таким образом:

* LLM генерирует 90% навыков без человека.
* Разработчики могут писать сложные навыки руками.

---

### **4. CI/CD и магазин навыков как основа безопасности**

* Все навыки проходят через **автоматизированный пайплайн**:

  1. Проверка прав и зависимостей.
  2. Прогон в тестовом окружении / эмуляторе.
  3. Подпись и публикация в магазине.

* Магазин управляет версиями SDK и навыков, как App Store.

---

### **5. Лёгкая возможность иерархии и переиспользования навыков**

* Навык может **вызвать другой навык** через SDK (`invoke_skill(skill_id, params)`).
* Это позволяет строить иерархию без сложного DSL-графа.
* Визуальный редактор на сервере может отображать эти связи как **граф**, но это всего лишь UI-надстройка.

### Skill Lifecycle

```plantuml
@startuml Skill_Lifecycle
!includeurl https://raw.githubusercontent.com/plantuml-stdlib/C4-PlantUML/master/C4_Component.puml

LAYOUT_WITH_LEGEND()

Container(skillMgr, "SkillManager", "Python", "Управляет жизненным циклом навыков")
Component(store, "Local Skill Store", "Filesystem", "Хранилище навыков")
Component(registry, "Skill Registry", "Git / AdaOS Server", "Централизованный каталог")
Component(executor, "SkillExecutor", "Python Sandbox", "Изолированное исполнение навыков")

Rel(skillMgr, registry, "Запрашивает метаинформацию / навык")
Rel(skillMgr, store, "Сохраняет/удаляет/обновляет")
Rel(skillMgr, executor, "Передаёт навык на запуск")
Rel(executor, store, "Читает код / настройки")
Rel(executor, skillMgr, "Сообщает результат")

@enduml

```

## Таблица взаимодействий с AdaOS Server

| Этап                    | Запрос от Reutilizer   | Ответ от AdaOS Server                    |
| ----------------------- | ---------------------- | ---------------------------------------- |
| 📡 Регистрация          | `POST /register` + ID  | `200 OK` + `config`, `skill_list`        |
| 🔁 Синхронизация        | `GET /skills/update`   | Список доступных обновлений              |
| ⬇️ Установка навыка     | `GET /skills/{id}`     | Архив с кодом + манифест                 |
| 📥 Отправка логов       | `POST /logs`           | `200 OK` или `retry`                     |
| 📤 Загрузка данных      | `POST /data` (sensors) | `200 OK` или `rules to trigger`          |
| 🔃 Обновление состояния | `PATCH /status`        | Могут быть переданы команды или сценарии |


### Общая архитектура
```plantuml
@startuml Hybrid_Approach
!includeurl https://raw.githubusercontent.com/plantuml-stdlib/C4-PlantUML/master/C4_Context.puml
!includeurl https://raw.githubusercontent.com/plantuml-stdlib/C4-PlantUML/master/C4_Container.puml

LAYOUT_WITH_LEGEND()

Person(user, "Пользователь", "Создаёт или использует навыки")

System_Boundary(s1, "AdaOS Server") {
  
  Container(ui, "Web UI / Voice UI", "React / Ionic", "Интерфейс для управления навыками и устройствами")
  Container(api, "API Gateway", "FastAPI / GraphQL", "Единая точка входа для UI и LLM")
  
  Container(store, "Skill Store", "PostgreSQL + S3", "Хранилище навыков, версий и прав доступа")
  Container(ci, "CI/CD Pipeline", "GitHub Actions / Drone CI", "Проверка и подписание навыков")
  
  Container(llm, "LLM Engine", "ChatGPT / Ollama", "Генерация и модификация навыков")
  
  Container(deviceReg, "Device Registry", "Redis + PostgreSQL", "Регистрация и статус устройств")
  Container(mqtt, "Messaging Broker", "MQTT / WebSocket", "Команды и обновления для устройств")

}

System_Boundary(s2, "Устройство с AdaOS") {
  Container(runtime, "Skill Runtime", "Python + Skill SDK", "Исполнение навыков с системой прав")
  Container(updater, "Updater", "Git / HTTPS", "Обновление навыков и ядра системы")
}

Rel(user, ui, "Управляет навыками и устройствами")
Rel(ui, api, "Вызывает")
Rel(api, llm, "Запрос на генерацию навыка")
Rel(api, store, "Чтение / запись навыков")
Rel(api, ci, "Инициирует проверку и подписание навыка")
Rel(api, deviceReg, "Обновляет статусы устройств")
Rel(api, mqtt, "Отправляет команды")
Rel(store, ci, "Отдаёт исходники для проверки")

Rel(mqtt, runtime, "Доставляет команды и обновления")
Rel(updater, store, "Скачивает обновлённые навыки")
Rel(runtime, store, "Устанавливает навыки через SDK")

@enduml

```

## **Примеры использования CLI**

### Создание навыка:

```bash
python cli.py skill request "Ада, научись ставить будильник"
```

### Список навыков:

```bash
python cli.py skill list
```

### Версии навыка:

```bash
python cli.py skill versions AlarmSkill
```

### Запуск теста вручную:

```bash
python cli.py test run runtime/tests/test_alarm.yaml
```

### Откат последнего коммита:

```bash
python cli.py skill rollback
```

### Логи Runtime:

```bash
python cli.py runtime logs
```

## Запуск локальной версии

```bash
# Сборка
docker-compose build

# Запуск
docker-compose up -d

# Войти внутрь контейнера и запустить CLI
docker exec -it adaos bash
python cli.py skill list

```
