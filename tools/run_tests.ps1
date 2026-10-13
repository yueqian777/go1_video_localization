$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$py = Join-Path $root 'runtime\python312\python.exe'
if (-not (Test-Path $py)) { throw "D-drive venv missing: $py" }
& $py (Join-Path $root 'tests\run_all.py')
