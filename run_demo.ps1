$ErrorActionPreference = "Stop"
Set-Location -LiteralPath $PSScriptRoot

if (-not (Get-Command python -ErrorAction SilentlyContinue)) {
    Write-Host "Python is not installed or is not available in PATH." -ForegroundColor Red
    Write-Host "Install Python 3.11 or newer from https://www.python.org/downloads/"
    exit 1
}

if (-not (Test-Path ".venv")) {
    python -m venv .venv
}

$python = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"
& $python -m pip install --upgrade pip
& $python -m pip install -r requirements-demo.txt

$env:DEMO_MODE = "true"
$env:ENV = "development"
$env:DEBUG = "false"
$env:SECRET_KEY = "demo-local-secret-not-for-production"
& $python manage.py migrate --noinput
& $python manage.py seed_demo

Write-Host ""
Write-Host "Demo ready: http://127.0.0.1:8000/es/" -ForegroundColor Green
Write-Host "Press Ctrl+C to stop it."
& $python manage.py runserver 127.0.0.1:8000
