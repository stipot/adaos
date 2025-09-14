# Контекст SDK

SDK предоставляет упрощённый доступ к `AgentContext`.

## Пример

```python
from adaos.apps.bootstrap import AgentContext

ctx = AgentContext()

# использовать git
repo = ctx.git.clone("https://github.com/example/skill.git")

# работать с secrets
ctx.secrets.write("openai/api_key", "sk-...")
````

## Зачем нужен

* Для тестов и отладки.
* Для разработки навыков без необходимости писать интеграции руками.
* Для унификации доступа к сервисам.
