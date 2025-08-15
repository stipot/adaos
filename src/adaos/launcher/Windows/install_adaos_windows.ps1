param(
  [string]$RepoUrl = "https://github.com/your-org/adaos.git",
  [string]$Branch = "main",
  [string]$InstallDir = "$env:USERPROFILE\adaos",
  [string]$PythonExe = "python",                 # или полный путь к python.exe
  [string]$AdaosToken = "dev-local-token",
  [int]$GatewayPort = 8777,
  [int]$CorePort = 8788,
  [string]$Lang = "en"
)

$ErrorActionPreference = "Stop"

function Ensure-Git {
  if (-not (Get-Command git -ErrorAction SilentlyContinue)) {
    throw "Git не найден. Установите Git и перезапустите скрипт."
  }
}

Ensure-Git

# --- Клонирование/обновление ---
if (Test-Path "$InstallDir\.git") {
  Write-Host "[*] Обновляю репозиторий: $InstallDir"
  git -C $InstallDir fetch --all
  git -C $InstallDir checkout $Branch
  git -C $InstallDir pull --ff-only
} else {
  Write-Host "[*] Клонирую $RepoUrl -> $InstallDir"
  git clone --branch $Branch $RepoUrl $InstallDir
}

# --- venv + установка ---
$VenvDir = Join-Path $InstallDir ".venv"
if (-not (Test-Path $VenvDir)) {
  & $PythonExe -m venv $VenvDir
}
$Py = Join-Path $VenvDir "Scripts\python.exe"
$Pip = Join-Path $VenvDir "Scripts\pip.exe"

& $Py -m pip install -U pip setuptools wheel
& $Pip install $InstallDir   # можно заменить на: & $Pip install -e $InstallDir

# --- стартовый скрипт sentinel ---
$BinDir = "$env:USERPROFILE\AppData\Local\AdaOS\bin"
New-Item -ItemType Directory -Force -Path $BinDir | Out-Null

$StartPs1 = Join-Path $BinDir "start-sentinel.ps1"
@"
`$env:ADAOS_TOKEN = "$AdaosToken"
`$env:ADAOS_GW_PORT = "$GatewayPort"
`$env:ADAOS_TARGET_PORT = "$CorePort"
`$env:ADAOS_CMD = "adaos start --lang $Lang --http 127.0.0.1:$CorePort"
Start-Process -FilePath "$Py" -ArgumentList "-m","adaos.launcher.sentinel" -WindowStyle Hidden
"@ | Set-Content -Encoding UTF8 $StartPs1

# --- Планировщик задач: автозапуск при логоне ---
$TaskName = "AdaOS Sentinel"
try {
  Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue
} catch {}

$Action = New-ScheduledTaskAction -Execute "powershell.exe" -Argument "-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$StartPs1`""
$Trigger = New-ScheduledTaskTrigger -AtLogOn
$Principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType InteractiveToken -RunLevel Highest
$Task = New-ScheduledTask -Action $Action -Trigger $Trigger -Principal $Principal -Settings (New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -StartWhenAvailable)
Register-ScheduledTask -TaskName $TaskName -InputObject $Task | Out-Null

Write-Host "`n=== Готово ==="
Write-Host "Sentinel слушает:   http://127.0.0.1:$GatewayPort"
Write-Host "Ядро (lazy boot) на: http://127.0.0.1:$CorePort"
Write-Host "Токен:               $AdaosToken"
Write-Host "`nПроверка (PowerShell):"
Write-Host "  Invoke-RestMethod -Headers @{ 'X-AdaOS-Token' = '$AdaosToken' } -Method Post -Uri http://127.0.0.1:$GatewayPort/api/say -Body '{""text"":""Hello from AdaOS!""}'"
