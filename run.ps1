# Start WealthTrack.
#   .\run.ps1            -> http://127.0.0.1:8000
#   .\run.ps1 -Port 9000 -> pick another port

param([int]$Port = 8000)

$ErrorActionPreference = "Stop"
$python = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"

if (-not (Test-Path $python)) {
    Write-Host "No virtual environment found. Creating one..." -ForegroundColor Yellow
    python -m venv (Join-Path $PSScriptRoot ".venv")
    & $python -m pip install --upgrade pip
    & $python -m pip install -r (Join-Path $PSScriptRoot "requirements-dev.txt")
}

Write-Host "WealthTrack running at http://127.0.0.1:$Port  (Ctrl+C to stop)" -ForegroundColor Green
& $python -m uvicorn app.main:app --host 127.0.0.1 --port $Port
