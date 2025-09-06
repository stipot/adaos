# Архитектура

```mermaid
flowchart LR
  Agent((Agent))
  Runtime[[Runtime]]
  Scheduler[[Scheduler]]
  CLI[/CLI/]
  API[/API/]
  Tests[/Tests/]
  Docs[/Docs/]
  Skills[(Skills)]
  Scenarios[(Scenarios)]

  Agent --> Runtime --> Scheduler
  CLI --> Agent
  API --> Agent
  Tests --> Runtime
  Docs --> CLI
  Runtime --> Skills
  Runtime --> Scenarios
```

## Политики/безопасность — как это работает

По умолчанию core получает secrets.read/secrets.write. Если потом появятся скиллы, которым нельзя читать все секреты — заведём subject="skill:<id>" и выдадим ограниченный набор ключей (это легко расширить в SecretsService).

CLI никогда не пишет значения секретов в логи/события; только в stdout по --show.

Основной бэкенд — OS keyring; если он недоступен (CI/минимальная ОС), падаем в FileVault с шифрованием Fernet и мастер-ключом из keyring/ENV.
