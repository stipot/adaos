# tools/bootstrap.ps1
# Унифицированный bootstrap с выбором версии Python

$ErrorActionPreference = "Stop"
$subPath = "src/adaos/integrations/inimatic"

function Get-PythonCandidates {
    # Собираем все пути из `py -0p`, не полагаясь на формат префикса (-3.11-64, -V:3.12, vendor tags и т.п.)
    $cands = @()

    if (Get-Command py -ErrorAction SilentlyContinue) {
        $lines = & py -0p 2>$null
        foreach ($ln in $lines) {
            # вытаскиваем путь к python.exe с конца строки
            if ($ln -match "(?<path>[A-Za-z]:\\.+?python\.exe)\s*$") {
                $path = $Matches["path"]
                # спрашиваем сам интерпретатор: версия и архитектура
                try {
                    $out = & "$path" -c "import sys,platform; print(f'{sys.version_info[0]}.{sys.version_info[1]}|{platform.architecture()[0]}')" 2>$null
                    $ver,$arch = $out.Split("|")
                    $v = [version]$ver
                    $cands += [pscustomobject]@{ Version=$v; Arch=($arch -replace '-bit',''); Path=$path }
                } catch { }
            }
        }
    }

    # fallback: python из PATH
    if (-not $cands -and (Get-Command python -ErrorAction SilentlyContinue)) {
        try {
            $out = & python -c "import sys,platform; print(f'{sys.version_info[0]}.{sys.version_info[1]}|{platform.architecture()[0]}')" 2>$null
            $ver,$arch = $out.Split("|")
            $v = [version]$ver
            $cands += [pscustomobject]@{ Version=$v; Arch=($arch -replace '-bit',''); Path=(Get-Command python).Source }
        } catch { }
    }

    # сортируем по версии (новее выше)
    return $cands | Sort-Object Version -Descending -Unique
}

# ---- вызов и выбор версии ----
Write-Host "▶ Поиск установленных версий Python..." -ForegroundColor Cyan
$pyCands = Get-PythonCandidates
if (!$pyCands -or $pyCands.Count -eq 0) {
    Write-Host "❌ Не найден ни один установленный Python. Поставьте Python 3.11+ и перезапустите." -ForegroundColor Red
    exit 1
}

$default = $pyCands | Where-Object { $_.Version -ge [version]"3.11" -and $_.Arch -eq "x64" } | Select-Object -First 1
if (-not $default) { $default = $pyCands | Where-Object { $_.Version -ge [version]"3.11" } | Select-Object -First 1 }
if (-not $default) { $default = $pyCands | Select-Object -First 1 }

Write-Host ""
Write-Host "Доступные Python:" -ForegroundColor Cyan
for ($i=0; $i -lt $pyCands.Count; $i++) {
    $mark = if ($pyCands[$i].Path -eq $default.Path) {" (по умолчанию)"} else {""}
    Write-Host ("  [{0}] {1} {2}  ->  {3}{4}" -f $i, $pyCands[$i].Version, $pyCands[$i].Arch, $pyCands[$i].Path, $mark) -ForegroundColor Yellow
}

$choice = Read-Host "Выберите номер Python для .venv (Enter = по умолчанию)"
if ([string]::IsNullOrWhiteSpace($choice)) {
    $chosen = $default
} else {
    if ($choice -notmatch '^\d+$' -or [int]$choice -ge $pyCands.Count) {
        Write-Host "⚠ Некорректный выбор. Использую вариант по умолчанию." -ForegroundColor DarkYellow
        $chosen = $default
    } else {
        $chosen = $pyCands[[int]$choice]
    }
}
Write-Host ("▶ Выбран Python {0} {1} -> {2}" -f $chosen.Version, $chosen.Arch, $chosen.Path) -ForegroundColor Green

# Инициализация submodule (Inimatic)
if (!(Test-Path $subPath)) {
    Write-Host "▶ Инициализируем субмодуль Inimatic..." -ForegroundColor Cyan
    git submodule update --init --recursive
}
if (!(Test-Path $subPath)) {
    Write-Host "❌ Не найден субмодуль '$subPath'. Проверь .gitmodules → path=..." -ForegroundColor Red
    exit 1
}

# Пересоздать venv, если он есть и на другой версии
function Get-VenvPyVersion {
    if (Test-Path ".venv\Scripts\python.exe") {
        & .\.venv\Scripts\python.exe -c "import sys;print(f'{sys.version_info[0]}.{sys.version_info[1]}')" 2>$null
    } else { return $null }
}
$venvVer = Get-VenvPyVersion
if ($venvVer) {
    if ([version]$venvVer -ne $chosen.Version) {
        Write-Host "▶ Найден .venv на $venvVer — пересоздаю на $($chosen.Version)..." -ForegroundColor Yellow
        try { Remove-Item -Recurse -Force .venv } catch {}
    }
}

if (!(Test-Path ".venv")) {
    Write-Host "▶ Создаю .venv выбранным интерпретатором..." -ForegroundColor Cyan
    & $chosen.Path -m venv .venv
}

Write-Host "▶ Устанавливаю Python-зависимости (editable)..." -ForegroundColor Cyan
.\.venv\Scripts\python.exe -m pip install -U pip
.\.venv\Scripts\python.exe -m pip install -e .
if ($LASTEXITCODE -ne 0) {
    Write-Host "⛔ Не удалось установить Python-зависимости. Проверь вывод выше." -ForegroundColor Red
    exit 1
}

# Node deps для Inimatic (robust install с fallback)
Push-Location $subPath
Write-Host "▶ Устанавливаю фронтовые зависимости..." -ForegroundColor Cyan

$used = ""
if (Get-Command pnpm -ErrorAction SilentlyContinue) {
    # pnpm предпочтителен, если уже установлен
    pnpm install
    $used = "pnpm install"
} else {
    $hasLock = Test-Path "package-lock.json"
    if ($hasLock) {
        # сначала пробуем строго
        $ci = $true
        try {
            npm ci
            $used = "npm ci"
        } catch {
            $ci = $false
        }
        if (-not $ci) {
            Write-Host "⚠ npm ci не сработал (lock рассинхронизирован). Делаю мягкую установку: npm install" -ForegroundColor Yellow
            npm install
            $used = "npm install"
        }
    } else {
        npm install
        $used = "npm install"
    }
}
if ($LASTEXITCODE -ne 0) {
    Pop-Location
    Write-Host "⛔ Установка фронтовых зависимостей завершилась ошибкой." -ForegroundColor Red
    Write-Host "   Попробуй удалить node_modules и lock, затем повторить:" -ForegroundColor Yellow
    Write-Host "     cd $subPath" -ForegroundColor Yellow
    Write-Host "     rmdir /s /q node_modules; del package-lock.json; npm install" -ForegroundColor Yellow
    exit 1
}
Write-Host "✓ Зависимости установлены ($used)" -ForegroundColor Green
Pop-Location

# .env (если есть пример)
if (!(Test-Path ".env") -and (Test-Path ".env.example")) { Copy-Item .env.example .env }


Write-Host @"
✅ Готово.

Дальше обычно запускают (в отдельных вкладках/окнах):
  1) Активируйте виртуальное окружение: .\.venv\Scripts\Activate.ps1
  2) CLI
     adaos --help
  3) API
     adaos api serve --host 127.0.0.1 --port 8777 --reload
  4) Backend (Inimatic)
     cd src\adaos\integrations\inimatic
     npm run start:api-dev
  5) Frontend (Inimatic)
     cd src\adaos\integrations\inimatic
     npm run start
Подсказки:
 • Список установленных Python: py -0p
 • Сменить версию venv: удалите .venv и перезапустите bootstrap, выбрав другой Python
"@