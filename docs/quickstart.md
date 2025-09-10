# Установка

```bash
git clone -b rev2026 https://github.com/stipot/adaos.git
cd adaos
git submodule update --init --recursive

# install dev core (optional)
pip install -e ".[dev]"

# mac/linux:
bash tools/bootstrap.sh
# windows (PowerShell):
./tools/bootstrap.ps1
. .\.venv\Scripts\Activate.ps1

# API и Web:
make dev        # just dev / npm run dev
# API: http://127.0.0.1:8777
# Web (Inimatic): http://127.0.0.1:810

adaos --help
```
