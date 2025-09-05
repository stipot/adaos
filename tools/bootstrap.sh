#!/usr/bin/env bash
# mac/linux
set -euo pipefail

python tools/check_env.py

# 1) обновить submodules (Inimatic)
git submodule update --init --recursive

# 2) Python venv + install (по pyproject.toml, setuptools)
python -m venv .venv
source .venv/bin/activate
python -m pip install -U pip
python -m pip install -e .    # editable install по pyproject.toml

# 3) Inimatic deps (npm сейчас; pnpm — опционально)
pushd src/adaos/integrations/inimatic
if command -v pnpm >/dev/null 2>&1; then
  pnpm install
else
  npm install
fi
popd

# 4) .env
[ -f .env ] || cp .env.example .env 2>/dev/null || true

cat <<EOF

✅ Bootstrap готов.

Запуски (mac/linux):
  API:     . .venv/bin/activate && adaos api serve --host 127.0.0.1 --port 8777 --reload
  Backend: (Inimatic) cd src/adaos/integrations/inimatic && npm run start:api-dev
  Front:   (Inimatic) cd src/adaos/integrations/inimatic && npm run start

Под VSCode используйте ваши tasks (redis, backend: ts-node, frontend: serve).
EOF
