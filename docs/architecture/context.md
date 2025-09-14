# AgentContext

`AgentContext` — это точка сборки ядра AdaOS.  
Он создаётся в `apps/bootstrap.py` и инкапсулирует все основные сервисы и политики.

---

## Назначение

- Централизовать доступ к портам и сервисам.  
- Исключить глобальные singletons и магические зависимости.  
- Давать сервисам **только нужные порты**, а не весь контекст.  

---

## Состав AgentContext

### Настройки и пути

- **settings** — параметры запуска (profile, base_dir, monorepo, режим тестирования).  
- **paths** — `PathProvider`, формирует директории:  
  - skills_dir  
  - scenarios_dir  
  - state  
  - cache  
  - logs  

### Системные компоненты

- **bus** — событийная шина.  
- **proc** — процесс-менеджер (управление sandbox-процессами).  

### Политики

- **caps** — Capabilities (права на действия).  
- **net** — NetPolicy (разрешённые домены/сети).  
- **fs** — FSPolicy (разрешённые корни в файловой системе).  

### Хранилища

- **sql** — SQLite (основные таблицы).  
- **kv** — SQLiteKV (ключ-значение).  

### Внешние сервисы

- **git** — SecureGitClient (поверх CLI Git).  
- **secrets** — SecretsService (OS keyring или FileVault с шифрованием Fernet).  
- **sandbox** — SandboxService (изоляция выполнения).  

---

## Пример использования

```python
from adaos.apps.bootstrap import AgentContext

ctx = AgentContext()
skill = ctx.services.skills.get("example")

# подписка на события
async def handler(event):
    print("got event", event)

await ctx.bus.subscribe("sys.boot.start", handler)
````

---

## Принцип проектирования

- **Изоляция**: сервисы не получают весь `AgentContext`, а только свои зависимости.
- **Безопасность**: политики (net, fs, secrets) навешиваются в контексте, а не в самих навыках.
- **Тестируемость**: в тестах можно подменять отдельные порты (например, fake GitClient).

---
