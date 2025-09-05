$ErrorActionPreference = "Stop"

Write-Host "▶ Проверяем окружение..." -ForegroundColor Cyan
python tools/check_env.py
if ($LASTEXITCODE -ne 0) {
  Write-Host "⛔ Остановка: окружение не готово. Исправь замечания выше и запусти снова." -ForegroundColor Red
  exit 1
}

# путь к субмодулю
$subPath = "src/adaos/integrations/inimatic"
if (!(Test-Path $subPath)) {
  Write-Host "▶ Инициализируем субмодуль Inimatic..." -ForegroundColor Cyan
  git submodule update --init --recursive
}
if (!(Test-Path $subPath)) {
  Write-Host "❌ Не найден субмодуль '$subPath'. Проверь .gitmodules → path=... и перезапусти." -ForegroundColor Red
  exit 1
}

# venv: если есть .venv на старом Python — пересоздадим на 3.11
function Get-VenvPyVersion {
  if (Test-Path ".venv\Scripts\python.exe") {
    & .\.venv\Scripts\python.exe -c "import sys; print(f'{sys.version_info[0]}.{sys.version_info[1]}')" 2>$null
  } else { return $null }
}

$venvVer = Get-VenvPyVersion
if ($venvVer -and ([version]$venvVer -lt [version]"3.10")) {
  Write-Host "▶ Обнаружен .venv на Python $venvVer — пересоздаю на 3.11..." -ForegroundColor Yellow
  try { Remove-Item -Recurse -Force .venv } catch {}
  if (Get-Command py -ErrorAction SilentlyContinue) {
    py -3.11 -m venv .venv
  } else {
    Write-Host "⚠ Не найден лаунчер 'py'. Убедись, что установлен Python 3.11 и в PATH, иначе будет использован текущий 'python'." -ForegroundColor Yellow
    python -m venv .venv
  }
} elseif (-not $venvVer) {
  Write-Host "▶ Создаю .venv (рекомендуется Python 3.11)..." -ForegroundColor Cyan
  if (Get-Command py -ErrorAction SilentlyContinue) {
    py -3.11 -m venv .venv
  } else {
    python -m venv .venv
  }
}

Write-Host "▶ Устанавливаю Python-зависимости (editable)..." -ForegroundColor Cyan
.\.venv\Scripts\python.exe -m pip install -U pip
.\.venv\Scripts\python.exe -m pip install -e .
if ($LASTEXITCODE -ne 0) {
  Write-Host "⛔ Не удалось установить Python-зависимости. Проверь вывод выше." -ForegroundColor Red
  exit 1
}

# Node deps для Inimatic (без вызова ng)
Push-Location $subPath
Write-Host "▶ Устанавливаю фронтовые зависимости (npm/pnpm)..." -ForegroundColor Cyan
if (Get-Command pnpm -ErrorAction SilentlyContinue) {
  pnpm install
} else {
  if (Test-Path "package-lock.json") { npm ci } else { npm install }
}
if ($LASTEXITCODE -ne 0) {
  Pop-Location
  Write-Host "⛔ Не удалось установить фронтовые зависимости. Проверь вывод выше." -ForegroundColor Red
  exit 1
}
Pop-Location

# .env (если есть пример)
if (!(Test-Path ".env") -and (Test-Path ".env.example")) { Copy-Item .env.example .env }

Write-Host @"
✅ Готово.

запуск (в отдельных окнах/табах):
  0) Активируйте виртуальное окружение: .\.venv\Scripts\Activate.ps1
  1) API      adaos.exe api serve --host 127.0.0.1 --port 8777 --reload
  2) Backend  cd $subPath ; npm run start:api-dev
  3) Frontend cd $subPath ; npm run start

подсказки:
 • если снова подхватился не тот Python: `Remove-Item -Recurse -Force .venv ; py -3.11 -m venv .venv`
 • pnpm не обязателен — автоматически падаем на npm
 • никаких вызовов Angular CLI в bootstrap нет → ENOENT от `ng` больше не появится
"@
