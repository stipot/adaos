# tools/bootstrap.ps1
# Unified bootstrap for Windows PowerShell 5.1+

$ErrorActionPreference = "Stop"

function Get-PythonCandidates {
    $cands = @()

    if (Get-Command py -ErrorAction SilentlyContinue) {
        $lines = & py -0p 2>$null
        foreach ($ln in $lines) {
            if ($ln -match "(?<path>[A-Za-z]:\\.+?python\.exe)\s*$") {
                $path = $Matches["path"]
                try {
                    $out = & "$path" -c "import sys,platform; print(f'{sys.version_info[0]}.{sys.version_info[1]}|{platform.architecture()[0]}')" 2>$null
                    $ver,$arch = $out.Split("|")
                    $v = [version]$ver
                    $cands += [pscustomobject]@{
                        Version = $v
                        Arch    = ($arch -replace '-bit','')
                        Path    = $path
                    }
                }
                catch { }
            }
        }
    }

    if (-not $cands -and (Get-Command python -ErrorAction SilentlyContinue)) {
        try {
            $out = & python -c "import sys,platform; print(f'{sys.version_info[0]}.{sys.version_info[1]}|{platform.architecture()[0]}')" 2>$null
            $ver,$arch = $out.Split("|")
            $v = [version]$ver
            $cands += [pscustomobject]@{
                Version = $v
                Arch    = ($arch -replace '-bit','')
                Path    = (Get-Command python).Source
            }
        }
        catch { }
    }

    $cands | Sort-Object Version -Descending -Unique
}

Write-Host "Searching for installed Python..."
$pyCands = Get-PythonCandidates
if (-not $pyCands -or $pyCands.Count -eq 0) {
    Write-Host "No Python found. Install Python 3.11+ and re-run." -ForegroundColor Red
    exit 1
}

$default = $pyCands | Where-Object { $_.Version -ge [version]"3.11" -and $_.Arch -eq "x64" } | Select-Object -First 1
if (-not $default) { $default = $pyCands | Where-Object { $_.Version -ge [version]"3.11" } | Select-Object -First 1 }
if (-not $default) { $default = $pyCands | Select-Object -First 1 }

Write-Host ""
Write-Host "Available Python:"
for ($i=0; $i -lt $pyCands.Count; $i++) {
    $mark = ""
    if ($pyCands[$i].Path -eq $default.Path) { $mark = " (default)" }
    Write-Host ("  [{0}] {1} {2} -> {3}{4}" -f $i, $pyCands[$i].Version, $pyCands[$i].Arch, $pyCands[$i].Path, $mark)
}

$choice = Read-Host "Pick index for .venv (Enter = default)"
if ([string]::IsNullOrWhiteSpace($choice)) {
    $chosen = $default
}
elseif ($choice -notmatch '^\d+$' -or [int]$choice -ge $pyCands.Count) {
    Write-Host "Invalid choice. Using default." -ForegroundColor Yellow
    $chosen = $default
}
else {
    $chosen = $pyCands[[int]$choice]
}
Write-Host ("Using Python {0} {1} -> {2}" -f $chosen.Version, $chosen.Arch, $chosen.Path) -ForegroundColor Green


function Get-VenvPyVersion {
    if (Test-Path ".venv\Scripts\python.exe") {
        & .\.venv\Scripts\python.exe -c "import sys;print(f'{sys.version_info[0]}.{sys.version_info[1]}')" 2>$null
    }
    else {
        return $null
    }
}

$venvVer = Get-VenvPyVersion
if ($venvVer) {
    if ([version]$venvVer -ne $chosen.Version) {
        Write-Host "Existing .venv is $venvVer; recreating for $($chosen.Version)..."
        try { Remove-Item -Recurse -Force .venv } catch { }
    }
}

if (!(Test-Path ".venv")) {
    Write-Host "Creating .venv..."
    & $chosen.Path -m venv .venv
    if ($LASTEXITCODE -ne 0) { Write-Host "Failed to create venv." -ForegroundColor Red; exit 1 }
}

Write-Host "Installing Python deps (editable)..."
.\.venv\Scripts\python.exe -m pip install -U pip
if ($LASTEXITCODE -ne 0) { Write-Host "pip upgrade failed." -ForegroundColor Red; exit 1 }
.\.venv\Scripts\python.exe -m pip install -e .[dev]
if ($LASTEXITCODE -ne 0) { Write-Host "pip install -e . failed." -ForegroundColor Red; exit 1 }

# Frontend deps
Push-Location $subPath
Write-Host "Installing frontend deps..."

$used = ""
if (Get-Command pnpm -ErrorAction SilentlyContinue) {
    pnpm install
    if ($LASTEXITCODE -ne 0) {
        Pop-Location
        Write-Host "pnpm install failed." -ForegroundColor Red
        exit 1
    }
    $used = "pnpm install"
}
else {
    if (Test-Path "package-lock.json") {
        npm ci
        if ($LASTEXITCODE -ne 0) {
            Write-Host "npm ci failed; falling back to npm install." -ForegroundColor Yellow
            npm install
            if ($LASTEXITCODE -ne 0) {
                Pop-Location
                Write-Host "npm install failed." -ForegroundColor Red
                exit 1
            }
            $used = "npm install"
        }
        else {
            $used = "npm ci"
        }
    }
    else {
        npm install
        if ($LASTEXITCODE -ne 0) {
            Pop-Location
            Write-Host "npm install failed." -ForegroundColor Red
            exit 1
        }
        $used = "npm install"
    }
}
Pop-Location
Write-Host ("Frontend deps installed ({0})." -f $used) -ForegroundColor Green

# .env from example
if (!(Test-Path ".env") -and (Test-Path ".env.example")) {
    Copy-Item .env.example .env
}

$helpText = @'
READY.

Next steps (in separate terminals):
  1) Activate venv:
     .\.venv\Scripts\Activate.ps1
  2) CLI:
     adaos --help
  3) API:
     adaos api serve --host 127.0.0.1 --port 8777 --reload
  4) Backend (Inimatic):
     cd src\adaos\integrations\inimatic
     npm run start:api-dev
  5) Frontend (Inimatic):
     cd src\adaos\integrations\inimatic
     npm run start

Tips:
 • List installed Python: py -0p
 • Switch venv version: delete .venv and re-run bootstrap
'@
Write-Host $helpText
