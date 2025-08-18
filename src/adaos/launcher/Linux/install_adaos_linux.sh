#!/usr/bin/env bash
set -euo pipefail

# === Параметры (подправьте при необходимости) ===
REPO_URL="${REPO_URL:-https://github.com/your-org/adaos.git}"
BRANCH="${BRANCH:-main}"
INSTALL_DIR="${INSTALL_DIR:-$HOME/adaos}"
VENV_DIR="${VENV_DIR:-$INSTALL_DIR/.venv}"
PYTHON_BIN="${PYTHON_BIN:-python3}"
ADAOS_TOKEN="${ADAOS_TOKEN:-dev-local-token}"
GW_PORT="${ADAOS_GW_PORT:-8777}"        # sentinel вход
CORE_PORT="${ADAOS_TARGET_PORT:-8788}"  # ядро AdaOS http
LANG="${ADAOS_LANG:-en}"

# === Зависимости (минимум) ===
if ! command -v git >/dev/null 2>&1; then
  echo "[*] Устанавливаю git..."
  if command -v apt >/dev/null 2>&1; then sudo apt update && sudo apt install -y git
  else echo "Установите git вручную"; exit 1; fi
fi

if ! command -v $PYTHON_BIN >/dev/null 2>&1; then
  echo "Требуется Python 3.8+ (найдено: отсутствует)"; exit 1
fi

# === Клонирование/обновление ===
if [[ -d "$INSTALL_DIR/.git" ]]; then
  echo "[*] Репозиторий найден, обновляю: $INSTALL_DIR"
  git -C "$INSTALL_DIR" fetch --all
  git -C "$INSTALL_DIR" checkout "$BRANCH"
  git -C "$INSTALL_DIR" pull --ff-only
else
  echo "[*] Клонирую $REPO_URL → $INSTALL_DIR"
  git clone --branch "$BRANCH" "$REPO_URL" "$INSTALL_DIR"
fi

# === Виртуальное окружение + установка ===
if [[ ! -d "$VENV_DIR" ]]; then
  echo "[*] Создаю venv: $VENV_DIR"
  $PYTHON_BIN -m venv "$VENV_DIR"
fi

# shellcheck disable=SC1090
source "$VENV_DIR/bin/activate"
python -m pip install -U pip setuptools wheel
echo "[*] pip install ."
pip install "$INSTALL_DIR"  # можно заменить на "pip install -e $INSTALL_DIR" для editable

# === Тонкий launcher-скрипт для sentinel ===
BIN_DIR="$HOME/.local/bin"
mkdir -p "$BIN_DIR"
SENTINEL_BIN="$BIN_DIR/adaos-sentinel"

cat > "$SENTINEL_BIN" <<EOF
#!/usr/bin/env bash
set -e
source "$VENV_DIR/bin/activate"
export ADAOS_TOKEN="$ADAOS_TOKEN"
export ADAOS_GW_PORT="$GW_PORT"
export ADAOS_TARGET_PORT="$CORE_PORT"
# ядро будет подниматься лениво; команда старта:
export ADAOS_CMD="adaos start --lang $LANG --http 127.0.0.1:$CORE_PORT"
exec python -m adaos.launcher.sentinel
EOF
chmod +x "$SENTINEL_BIN"
echo "[*] sentinel launcher: $SENTINEL_BIN"

# === systemd user service ===
SYSTEMD_DIR="$HOME/.config/systemd/user"
mkdir -p "$SYSTEMD_DIR"

SERVICE_FILE="$SYSTEMD_DIR/adaos-sentinel.service"
cat > "$SERVICE_FILE" <<EOF
[Unit]
Description=AdaOS Sentinel (user)
After=network-online.target

[Service]
Type=simple
ExecStart=$SENTINEL_BIN
Restart=on-failure
RestartSec=3
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=default.target
EOF

systemctl --user daemon-reload
systemctl --user enable --now adaos-sentinel.service

echo
echo "=== Готово ==="
echo "Sentinel слушает:   http://127.0.0.1:$GW_PORT"
echo "Ядро (lazy boot) на: http://127.0.0.1:$CORE_PORT"
echo "Токен:               $ADAOS_TOKEN"
echo
echo "Проверка:"
echo "  curl -H 'X-AdaOS-Token: $ADAOS_TOKEN' -X POST http://127.0.0.1:$GW_PORT/api/say -d '{\"text\":\"Hello from AdaOS!\"}'"
# TODO “socket activation” на Linux вместо sentinel