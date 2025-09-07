#!/usr/bin/env bash
# tools/bootstrap.sh — унифицированный bootstrap (macOS/Linux)
set -euo pipefail

SUBMODULE_PATH="src/adaos/integrations/inimatic"

# --- helpers ---------------------------------------------------------------

log()   { printf "\033[36m▶ %s\033[0m\n" "$*"; }
ok()    { printf "\033[32m✓ %s\033[0m\n" "$*"; }
warn()  { printf "\033[33m⚠ %s\033[0m\n" "$*"; }
fail()  { printf "\033[31m⛔ %s\033[0m\n" "$*"; exit 1; }

have()  { command -v "$1" >/dev/null 2>&1; }

# return "maj.min path" per line
discover_python() {
  local found=0
  # 1) pyenv
  if have pyenv; then
    while read -r v; do
      [[ -z "$v" || "$v" == system* ]] && continue
      local p; p="$(pyenv which -a python 2>/dev/null | grep "/$v/" | head -n1 || true)"
      [[ -x "$p" ]] && { echo "$v $p"; found=1; }
    done < <(pyenv versions --bare 2>/dev/null)
  fi
  # 2) common python3.x on PATH
  for x in 3.12 3.11 3.10 3.9; do
    if have "python$x"; then
      echo "$x $(command -v python$x)"; found=1
    fi
  done
  # 3) plain python3
  if have python3; then
    local v; v="$(python3 -c 'import sys;print(f"{sys.version_info[0]}.{sys.version_info[1]}")')" || true
    [[ -n "${v:-}" ]] && echo "$v $(command -v python3)" && found=1
  fi
  # 4) fallback python
  if [[ $found -eq 0 && $(have python; echo $?) -eq 0 ]]; then
    local v; v="$(python -c 'import sys;print(f"{sys.version_info[0]}.{sys.version_info[1]}")')" || true
    [[ -n "${v:-}" ]] && echo "$v $(command -v python)"
  fi
}

choose_python() {
  mapfile -t CANDS < <(discover_python | sort -Vr) || true
  [[ ${#CANDS[@]} -eq 0 ]] && fail "Python не найден. Установите 3.11+ и повторите."
  log "Найдены интерпретаторы Python:"
  local i=0
  for line in "${CANDS[@]}"; do
    printf "  [%d] %s\n" "$i" "$line"
    ((i = i + 1))
  done
  # default: первый с версией >=3.11, иначе [0]
  local def_idx=0
  for idx in "${!CANDS[@]}"; do
    [[ "${CANDS[$idx]}" =~ ^(3\.11|3\.12) ]] && { def_idx=$idx; break; }
  done
  read -r -p "Выберите номер Python для venv (Enter = ${def_idx}): " CHOICE
  [[ -z "${CHOICE:-}" ]] && CHOICE=$def_idx
  [[ "$CHOICE" =~ ^[0-9]+$ ]] || CHOICE=$def_idx
  local sel="${CANDS[$CHOICE]}"
  PY_VER="${sel%% *}"
  PY_BIN="${sel#* }"
  log "Выбрано: Python ${PY_VER} -> ${PY_BIN}"
  # hard guard
  [[ "${PY_VER%%.*}" -ge 3 && "${PY_VER#*.}" -ge 10 ]] || fail "Нужен Python ≥ 3.10 (рекомендуем 3.11+)."
}

smart_npm_install() {
  # prefers pnpm if present; else tries npm ci -> falls back to npm install
  if have pnpm; then
    pnpm install
    USED_PKG_CMD="pnpm install"
    return
  fi
  if [[ -f package-lock.json ]]; then
    if npm ci; then
      USED_PKG_CMD="npm ci"
    else
      warn "npm ci не сработал (lock рассинхронизирован). Делаю: npm install"
      npm install
      USED_PKG_CMD="npm install"
    fi
  else
    npm install
    USED_PKG_CMD="npm install"
  fi
}

open_subshell_help() {
  # if BOOTSTRAP_OPEN_SUBSHELL=1 then open interactive subshell with venv activated
  [[ "${BOOTSTRAP_OPEN_SUBSHELL:-0}" != "1" ]] && return 0
  local help_text
  read -r -d '' help_text <<'EOF' || true
✅ готово. venv активирован в этой оболочке.

обычно далее запускают (в отдельных вкладках/окнах):

  1) API
     adaos api serve --host 127.0.0.1 --port 8777 --reload

  2) Backend (Inimatic)
     cd src/adaos/integrations/inimatic
     npm run start:api-dev

  3) Frontend (Inimatic)
     cd src/adaos/integrations/inimatic
     npm run start

подсказки:
 • сменить версию venv — удалите .venv и перезапустите bootstrap
 • строгая проверка lock: BOOTSTRAP_STRICT_LOCK=1 (тогда падать, если npm ci не прошёл)
EOF
  # start an interactive subshell with venv sourced and help printed
  if [[ -n "${SHELL:-}" && -x "$SHELL" ]]; then
    "$SHELL" --rcfile <(printf 'source .venv/bin/activate\nprintf "%s\n" "%s"\n' "$help_text" "$help_text") -i
  else
    bash --rcfile <(printf 'source .venv/bin/activate\nprintf "%s\n" "%s"\n' "$help_text" "$help_text") -i
  fi
}

# --- run -------------------------------------------------------------------

log "Проверяю и выбираю Python…"
choose_python

log "Инициализирую submodule (Inimatic)…"
[[ -d "$SUBMODULE_PATH" ]] || git submodule update --init --recursive
[[ -d "$SUBMODULE_PATH" ]] || fail "Субмодуль не найден по пути '$SUBMODULE_PATH'. Проверь .gitmodules → path=…"

log "Создаю venv (если нужно)…"
if [[ -d .venv ]]; then
  VENV_VER="$(. .venv/bin/activate >/dev/null 2>&1 && python -c 'import sys;print(f"{sys.version_info[0]}.{sys.version_info[1]}")' || true)"
  if [[ -z "${VENV_VER:-}" || "$VENV_VER" != "$PY_VER" ]]; then
    warn "Обнаружен .venv на $VENV_VER — пересоздаю на $PY_VER…"
    rm -rf .venv
  fi
fi
[[ -d .venv ]] || "$PY_BIN" -m venv .venv

log "Ставлю Python-зависимости (editable)…"
. .venv/bin/activate
python -m pip install -U pip >/dev/null
python -m pip install -e .[dev] || fail "Не удалось установить Python-зависимости."

log "Ставлю фронтовые зависимости Inimatic…"
pushd "$SUBMODULE_PATH" >/dev/null
if [[ "${BOOTSTRAP_STRICT_LOCK:-0}" == "1" && -f package-lock.json && ! $(have pnpm; echo $?) -eq 0 ]]; then
  # строгий режим для npm: только npm ci
  npm ci || fail "npm ci не прошёл в строгом режиме. Синхронизируйте lock: npm install; commit lock."
  USED_PKG_CMD="npm ci"
else
  smart_npm_install
fi
ok "Зависимости установлены ($USED_PKG_CMD)"
popd >/dev/null

log "Готовлю .env…"
[[ -f .env || ! -f .env.example ]] || cp .env.example .env

ok  "Bootstrap завершён."
printf "\n\033[36m👉 Активируйте окружение:\033[0m \033[33msource .venv/bin/activate\033[0m\n"
printf "или экспортируйте \033[33mBOOTSTRAP_OPEN_SUBSHELL=1\033[0m и перезапустите скрипт, чтобы открыть интерактивный сабшелл.\n\n"

# опционально открыть новый интерактивный сабшелл с активированным venv и памяткой
open_subshell_help
