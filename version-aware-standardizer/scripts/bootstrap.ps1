<#
.SYNOPSIS
    One-shot setup for the Version-Aware Clinical Terminology Standardizer.

.DESCRIPTION
    Creates the Python virtual environment, installs dependencies, writes .env
    from .env.example if it is missing, clones Snowstorm into infra/, and
    applies the database migrations.

    It never downloads LOINC or SNOMED CT content: those are licence-controlled
    and must be placed in data/raw/ by hand.

.EXAMPLE
    .\scripts\bootstrap.ps1
    .\scripts\bootstrap.ps1 -SkipSnowstorm
#>
[CmdletBinding()]
param(
    [switch]$SkipSnowstorm,
    [switch]$SkipMigrations
)

$ErrorActionPreference = "Stop"

$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root
Write-Host "Project root: $Root" -ForegroundColor Cyan

# --- 1. Python -------------------------------------------------------------
$python = (Get-Command python -ErrorAction SilentlyContinue)
if ($null -eq $python) {
    throw "python was not found on PATH. Install Python 3.11 or newer first."
}
$version = (& python -c "import sys; print('.'.join(map(str, sys.version_info[:2])))")
Write-Host "Python $version" -ForegroundColor Cyan
if ([version]$version -lt [version]"3.11") {
    throw "Python 3.11+ is required; found $version."
}

# --- 2. Virtual environment ------------------------------------------------
$venv = Join-Path $Root ".venv"
if (-not (Test-Path $venv)) {
    Write-Host "Creating virtual environment ..." -ForegroundColor Cyan
    & python -m venv $venv
} else {
    Write-Host "Virtual environment already exists." -ForegroundColor DarkGray
}
$venvPython = Join-Path $venv "Scripts\python.exe"

Write-Host "Installing dependencies ..." -ForegroundColor Cyan
& $venvPython -m pip install --upgrade pip --quiet
& $venvPython -m pip install -r (Join-Path $Root "requirements.txt") --quiet
if ($LASTEXITCODE -ne 0) { throw "Dependency installation failed." }

# --- 3. .env ---------------------------------------------------------------
$envFile = Join-Path $Root ".env"
if (-not (Test-Path $envFile)) {
    Copy-Item (Join-Path $Root ".env.example") $envFile
    Write-Host "Created .env from .env.example -- edit the password before use." -ForegroundColor Yellow
} else {
    Write-Host ".env already exists; leaving it alone." -ForegroundColor DarkGray
}

# --- 4. Snowstorm ----------------------------------------------------------
if (-not $SkipSnowstorm) {
    $snowstorm = Join-Path $Root "infra\snowstorm"
    if (-not (Test-Path $snowstorm)) {
        Write-Host "Cloning Snowstorm (infrastructure only, never modified) ..." -ForegroundColor Cyan
        $gh = Get-Command gh -ErrorAction SilentlyContinue
        if ($null -ne $gh) {
            & gh repo clone IHTSDO/snowstorm $snowstorm
        } else {
            & git clone --depth 1 https://github.com/IHTSDO/snowstorm.git $snowstorm
        }
        if ($LASTEXITCODE -ne 0) { throw "Cloning Snowstorm failed." }
    } else {
        Write-Host "infra/snowstorm already present." -ForegroundColor DarkGray
    }

    # Elasticsearch needs a raised mmap count inside WSL2.
    $wsl = Get-Command wsl -ErrorAction SilentlyContinue
    if ($null -ne $wsl) {
        Write-Host "Raising vm.max_map_count for Elasticsearch (needs Docker Desktop) ..." -ForegroundColor Cyan
        try {
            & wsl -d docker-desktop sysctl -w vm.max_map_count=262144 | Out-Null
        } catch {
            Write-Host "  could not set vm.max_map_count; run this in an admin shell if Elasticsearch fails to start:" -ForegroundColor Yellow
            Write-Host "  wsl -d docker-desktop sysctl -w vm.max_map_count=262144" -ForegroundColor Yellow
        }
    }
}

# --- 5. Database -----------------------------------------------------------
if (-not $SkipMigrations) {
    Write-Host "Applying database migrations ..." -ForegroundColor Cyan
    & $venvPython -m alembic upgrade head
    if ($LASTEXITCODE -ne 0) {
        Write-Host "Migrations failed. Is PostgreSQL up? Try: docker compose up -d" -ForegroundColor Yellow
    }
}

# --- 6. Next steps ---------------------------------------------------------
Write-Host ""
Write-Host "Bootstrap complete." -ForegroundColor Green
Write-Host ""
Write-Host "Next steps:" -ForegroundColor Cyan
Write-Host "  1. docker compose up -d                       # PostgreSQL"
Write-Host "  2. cd infra\snowstorm; docker compose up -d; cd ..\..   # Snowstorm + Elasticsearch (~8 GB RAM)"
Write-Host "  3. Place the official releases (they are NOT downloaded for you):"
Write-Host "       data\raw\loinc\Loinc_<version>.zip        # free LOINC account"
Write-Host "       data\raw\snomed\SnomedCT_*RF2*.zip        # licensed affiliate access"
Write-Host "  4. .\.venv\Scripts\python.exe scripts\import_loinc.py  --file data\raw\loinc\<file>.zip  --version <version>"
Write-Host "  5. .\.venv\Scripts\python.exe scripts\import_snomed.py --file data\raw\snomed\<file>.zip --version <YYYYMMDD>"
Write-Host "  6. .\.venv\Scripts\python.exe -m uvicorn backend.app.main:app --reload   # http://localhost:8000/docs"
Write-Host ""
Write-Host "No terminology files yet? Run the full pipeline on synthetic data:" -ForegroundColor Cyan
Write-Host "  .\.venv\Scripts\python.exe scripts\demo_end_to_end.py"
