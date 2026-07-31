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
Schrijf-Stap "1/6" "Python controleren..."
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
Schrijf-Stap "2/6" "pip updaten..."
python -m pip install --upgrade pip --quiet
Schrijf-Ok "pip up-to-date"

# - Stap 3: Python packages installeren -------------
Schrijf-Stap "3/6" "Python packages installeren..."

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
Schrijf-Stap "4/6" "Playwright browser installeren (Microsoft Edge)..."
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
Schrijf-Stap "4/6" "Playwright browser installeren (Chromium)..."
Schrijf-Info "Dit kan enkele minuten duren..."
try {
    python -m playwright install chromium
    Schrijf-Ok "Chromium (Playwright) geinstalleerd"
} catch {
    Schrijf-Fout "Playwright Cromeium installatie mislukt."
    Schrijf-Info "Probeer handmatig: python -m playwright install chromium"
    Read-Host "Druk Enter om af te sluiten"
    exit 1
}

# - Stap 5: Configuratie + pakketkeuze -------------
Schrijf-Stap "5/6" "Configuratie instellen..."

# Werkt zowel via .\installeer.ps1 als via Invoke-Expression
if ($MyInvocation.MyCommand.Path) {
    $scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
} else {
    $scriptDir = (Get-Location).Path
}

$configPath  = Join-Path $scriptDir "config.json"
$examplePath = Join-Path $scriptDir "config.example.json"

if (-not (Test-Path $configPath)) {
    if (Test-Path $examplePath) {
        Copy-Item $examplePath $configPath
        Schrijf-Ok "config.json aangemaakt vanuit config.example.json"
        Schrijf-Info "Vul nadien nog je PeopleSoft-gebruikersnaam in via het dashboard (Inloggen)."
    } else {
        Schrijf-Fout "config.example.json niet gevonden - config.json moet je zelf aanmaken."
    }
}

if (Test-Path $configPath) {
    Write-Host ""
    Write-Host "  Welk pakket wil je installeren?" -ForegroundColor Yellow
    Write-Host "    1) Volledig pakket  - dashboard, thuisdialyse, RO-staalname, staalresultaten,"
    Write-Host "                          service rapporten, snelle werkorder (met achtergrond-scraper)"
    Write-Host "    2) Beperkt pakket   - enkel Service Rapporten + Snelle Werkorder"
    Write-Host "                          (geen achtergrond-scraper, geen OneDrive-sync)"
    $keuze = Read-Host "  Keuze (1 of 2, Enter = 1)"

    $modus = "volledig"
    if ($keuze -eq "2") { $modus = "beperkt" }

    try {
        $cfg = Get-Content $configPath -Raw -Encoding UTF8 | ConvertFrom-Json
        $cfg | Add-Member -NotePropertyName "modus" -NotePropertyValue $modus -Force
        $jsonTekst = $cfg | ConvertTo-Json -Depth 10
        # BELANGRIJK: geen 'Set-Content -Encoding UTF8', want dat schrijft een
        # BOM (byte-order-mark) aan het begin van het bestand. Python leest
        # config.json met open() zonder expliciete encoding, en breekt dan op
        # die onzichtbare BOM ("Expecting value"-fout bij json.load). Daarom
        # hier expliciet UTF8Encoding($false) = UTF-8 zonder BOM.
        $utf8ZonderBom = New-Object System.Text.UTF8Encoding $false
        [System.IO.File]::WriteAllText($configPath, $jsonTekst, $utf8ZonderBom)
        Schrijf-Ok "Pakket ingesteld op '$modus' in config.json"
    } catch {
        Schrijf-Fout "Kon 'modus' niet wegschrijven naar config.json: $_"
        Schrijf-Info "Zet dit veld dan zelf handmatig: `"modus`": `"$modus`""
    }
}

# - Stap 6: Snelkoppeling op bureaublad -------------
Schrijf-Stap "6/6" "Snelkoppeling aanmaken op bureaublad..."

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
