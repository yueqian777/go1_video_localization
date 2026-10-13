$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$py = Join-Path $root 'runtime\python312\python.exe'
if (-not (Test-Path $py)) { throw "D-drive venv missing: $py" }
$env:TEMP = Join-Path $root '.cache\tests'
$env:TMP = $env:TEMP
New-Item -ItemType Directory -Path $env:TEMP -Force | Out-Null
& $py (Join-Path $root 'tests\run_all.py')
if ($LASTEXITCODE -ne 0) { throw "Unit tests failed: $LASTEXITCODE" }
