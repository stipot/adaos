# Быстрый старт

## Установка

```bash
git clone -b rev2026 https://github.com/stipot/adaos.git
cd adaos
git submodule update --init --recursive

# установка в режиме разработки (опционально)
pip install -e ".[dev]"

# mac/linux:
bash tools/bootstrap.sh
# windows (PowerShell):
./tools/bootstrap.ps1
. .\.venv\Scripts\Activate.ps1
````

## Запуск

```bash
# API и Web
make dev        # запускает API и Inimatic
# API: http://127.0.0.1:8777
# Web (Inimatic): http://127.0.0.1:810
```

## CLI

```bash
adaos --help

# запустить API отдельно
adaos api serve --host 127.0.0.1 --port 8777

# запустить тесты в песочнице
adaos tests run
```
