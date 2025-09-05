$ErrorActionPreference = "Stop"

python tools/check_env.py

# 1) submodule
git submodule update --init --recursive

# 2) Python venv + install
if (!(Test-Path ".venv")) { python -m venv .venv }
.\.venv\Scripts\python.exe -m pip install -U pip
.\.venv\Scripts\python.exe -m pip install -e .

# 3) Inimatic deps
Push-Location src/adaos/integrations/inimatic
if (Get-Command pnpm -ErrorAction SilentlyContinue) {
  pnpm install
} else {
  npm install
}
Pop-Location

# 4) .env
if (!(Test-Path ".env") -and (Test-Path ".env.example")) { Copy-Item .env.example .env }

Write-Host @"
✅ Bootstrap готов.

Запуски (Windows):
  API:      .\.venv\Scripts\adaos.exe api serve --host 127.0.0.1 --port 8777 --reload
  Backend:  cd src\adaos\integrations\inimatic ; npm run start:api-dev
  Frontend: cd src\adaos\integrations\inimatic ; npm run start

В VSCode уже есть tasks: redis:docker / backend:ts-node / frontend:serve.
"@
