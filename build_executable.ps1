$ErrorActionPreference = "Stop"
Set-Location -LiteralPath $PSScriptRoot

if (-not (Test-Path ".build-venv")) {
    python -m venv .build-venv
}
$python = Join-Path $PSScriptRoot ".build-venv\Scripts\python.exe"

& $python -m pip install --upgrade pip
& $python -m pip install -r requirements-executable.txt

& $python -m PyInstaller --noconfirm --clean --onedir --noupx --console --name "MetricsExplorer-Demo" --add-data "authorsite;authorsite" --add-data "profiles;profiles" --add-data "locale;locale" --add-data "zz_tool_researchers_merged.py;." --collect-all django --collect-all whitenoise --collect-submodules authorsite --collect-submodules profiles --collect-all celery --collect-all kombu --collect-all billiard --collect-all vine --collect-all plotly --collect-all matplotlib --hidden-import psycopg launcher.py

Write-Host "Portable executable created in dist\MetricsExplorer-Demo" -ForegroundColor Green
