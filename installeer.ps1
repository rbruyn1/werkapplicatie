# ============================================================
#  Werkorder Dashboard - Installatie
#  Uitvoeren: rechtsklik -> "Uitvoeren met PowerShell"
#  Of in PowerShell: .\installeer.ps1
# ============================================================

$ErrorActionPreference = "Stop"
$Host.UI.RawUI.WindowTitle = "Werkorder Dashboard - Installatie"

function Schrijf-Stap($nr, $tekst) {
    Write-Host ""
    Write-Host "[$nr] $tekst" -ForegroundColor Cyan
}

function Schrijf-Ok($tekst)   { Write-Host "  [OK] $tekst" -ForegroundColor Green }
function Schrijf-Fout($tekst) { Write-Host "  [!!] $tekst" -ForegroundColor Red }
function Schrijf-Info($tekst) { Write-Host "    $tekst" -ForegroundColor Gray }

Write-Host ""
Write-Host "  +------------------------------------------------------+" -ForegroundColor White
Write-Host "  |       WERKORDER DASHBOARD - INSTALLATIE              |" -ForegroundColor White
Write-Host "  +------------------------------------------------------+" -ForegroundColor White

# - Stap 1: Python controleren / installeren --------
Schrijf-Stap "1/5" "Python controleren..."
$pythonOk = $false
try {
    $pyVer = python --version 2>&1
    if ($pyVer -match "Python") {
        Schrijf-Ok "$pyVer gevonden"
        $pythonOk = $true
    }
} catch {
    $pythonOk = $false
}

if (-not $pythonOk) {
    Schrijf-Info "Python niet gevonden - automatisch installeren via winget..."
    try {
        winget install --id Python.Python.3.14 --silent --accept-package-agreements --accept-source-agreements
        $env:Path = [System.Environment]::GetEnvironmentVariable("Path", "Machine") + ";" +
                    [System.Environment]::GetEnvironmentVariable("Path", "User")
        $pyVer = python --version 2>&1
        Schrijf-Ok "$pyVer geinstalleerd en klaar"
    } catch {
        Schrijf-Fout "Automatische installatie mislukt."
        Schrijf-Info "Installeer Python 3.14 handmatig via: https://www.python.org/downloads/"
        Schrijf-Info "Vink 'Add Python to PATH' aan tijdens de installatie."
        Read-Host "Druk Enter om af te sluiten"
        exit 1
    }
}

# - Stap 2: pip updaten ---------------------
Schrijf-Stap "2/5" "pip updaten..."
python -m pip install --upgrade pip --quiet
Schrijf-Ok "pip up-to-date"

# - Stap 3: Python packages installeren -------------
Schrijf-Stap "3/5" "Python packages installeren..."

$packages = @(
    @{ name = "flask";      pip = "flask>=3.0" },
    @{ name = "playwright"; pip = "playwright" },
    @{ name = "openpyxl";   pip = "openpyxl"   },
    @{ name = "watchdog";   pip = "watchdog"    },
    @{ name = "cryptography"; pip = "cryptography" }
)

foreach ($pkg in $packages) {
    try {
        python -m pip install $pkg.pip --quiet
        Schrijf-Ok $pkg.name
    } catch {
        Schrijf-Fout "Fout bij installeren van $($pkg.name)"
        Read-Host "Druk Enter om af te sluiten"
        exit 1
    }
}

# - Stap 4: Playwright Edge installeren -------------
Schrijf-Stap "4/5" "Playwright browser installeren (Microsoft Edge)..."
Schrijf-Info "Dit kan enkele minuten duren..."
try {
    python -m playwright install msedge
    Schrijf-Ok "Microsoft Edge (Playwright) geinstalleerd"
} catch {
    Schrijf-Fout "Playwright msedge installatie mislukt."
    Schrijf-Info "Probeer handmatig: python -m playwright install msedge"
    Read-Host "Druk Enter om af te sluiten"
    exit 1
}

# - Stap 4b: Playwright Chromium installeren -------------
Schrijf-Stap "4/5" "Playwright browser installeren (Microsoft Edge)..."
Schrijf-Info "Dit kan enkele minuten duren..."
try {
    python -m playwright install chromium
    Schrijf-Ok "Microsoft Edge (Playwright) geinstalleerd"
} catch {
    Schrijf-Fout "Playwright msedge installatie mislukt."
    Schrijf-Info "Probeer handmatig: python -m playwright install msedge"
    Read-Host "Druk Enter om af te sluiten"
    exit 1
}

# - Stap 5: Snelkoppeling op bureaublad -------------
Schrijf-Stap "5/5" "Snelkoppeling aanmaken op bureaublad..."

# Werkt zowel via .\installeer.ps1 als via Invoke-Expression
if ($MyInvocation.MyCommand.Path) {
    $scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
} else {
    $scriptDir = (Get-Location).Path
}

$startScript = Join-Path $scriptDir "app.py"
$desktop     = [Environment]::GetFolderPath("Desktop")
$shortcut    = Join-Path $desktop "Werkorder Dashboard.lnk"

try {
    $ws  = New-Object -ComObject WScript.Shell
    $sc  = $ws.CreateShortcut($shortcut)
    $sc.TargetPath       = (Get-Command python).Source
    $sc.Arguments        = "`"$startScript`""
    $sc.WorkingDirectory = $scriptDir
    $sc.Description      = "Werkorder Dashboard"
    $sc.Save()
    Schrijf-Ok "Snelkoppeling aangemaakt op bureaublad"
    Schrijf-Info "Verwijst naar: $startScript"
} catch {
    Schrijf-Info "Snelkoppeling kon niet aangemaakt worden - start handmatig via:"
    Schrijf-Info "python `"$startScript`""
}

# - Klaar --------------------------
Write-Host ""
Write-Host "  +------------------------------------------------------+" -ForegroundColor Green
Write-Host "  |   OK Installatie voltooid!                           |" -ForegroundColor Green
Write-Host "  |                                                      |" -ForegroundColor Green
Write-Host "  |   Start de app via de snelkoppeling op het           |" -ForegroundColor Green
Write-Host "  |   bureaublad, of met:  python app.py                 |" -ForegroundColor Green
Write-Host "  |                                                      |" -ForegroundColor Green
Write-Host "  |   Dashboard:  http://localhost:5000                  |" -ForegroundColor Green
Write-Host "  +------------------------------------------------------+" -ForegroundColor Green
Write-Host ""
Read-Host "Druk Enter om af te sluiten"