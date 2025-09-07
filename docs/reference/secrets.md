# Секреты

## Backends

- **KeyringVault** — хранит значения в OS keyring; индекс ключей — в SQLiteKV.
- **FileVault** — шифрованный `{BASE_DIR}/state/vault.json` (Fernet). Мастер-ключ из keyring или `ADAOS_VAULT_MASTER_KEY`.

## CLI

```bash
adaos secret set KEY VALUE
adaos secret get KEY [--show]
adaos secret list
adaos secret delete KEY
adaos secret export [--show]
adaos secret import file.json
````

## Политики

- Требуются `"secrets.read"`/`"secrets.write"`.
- Значения не логируются; экспорт по умолчанию маскирует.

- **KeyringVault** (основной)

  - Хранение значений в системном keyring (Windows Credential Locker / macOS Keychain / Secret Service).
  - Индекс ключей (для `list/export`) — в `SQLiteKV`.
  - Совместим с KV, у которого есть `get/set` или `get_json/set_json`.

- **FileVault** (фолбэк)

  - Файл `{BASE_DIR}/state/vault.json`, **зашифрованный Fernet**.
  - Мастер-ключ:

    - хранится в keyring (`service='adaos:master:<profile>'`), или
    - читается из `ADAOS_VAULT_MASTER_KEY` (для CI), или
    - генерируется при первом запуске.

## Сервис

`services/secrets/service.py` — обёртка с Capabilities:

- `put/get/delete/list/import_items/export_items`
- Требуются права: `"secrets.read"` и/или `"secrets.write"`.

## CLI

```bash
adaos secret set KEY VALUE            # создать/обновить
adaos secret get KEY [--show]         # по умолчанию маскирует
adaos secret list
adaos secret delete KEY
adaos secret export [--show]          # JSON (значения по умолчанию маскированы)
adaos secret import file.json
```

> CLI никогда не логирует значения секретов.
