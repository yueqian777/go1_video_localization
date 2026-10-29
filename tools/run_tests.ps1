$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$py = Join-Path $root 'runtime\python312\python.exe'
if (-not (Test-Path $py)) { throw "D-drive venv missing: $py" }
$drive = [System.IO.Path]::GetPathRoot((Resolve-Path $root).Path)
# MSYS2's assembler can fail when its temporary object path contains Chinese
# characters. Keep the test temp directory on the project drive, but ASCII.
$env:GO1LOC_TEST_TEMP = Join-Path $drive 'go1loc-test-temp'
$env:TEMP = $env:GO1LOC_TEST_TEMP
$env:TMP = $env:TEMP
New-Item -ItemType Directory -Path $env:TEMP -Force | Out-Null
& $py (Join-Path $root 'tests\run_all.py')
if ($LASTEXITCODE -ne 0) { throw "Unit tests failed: $LASTEXITCODE" }
