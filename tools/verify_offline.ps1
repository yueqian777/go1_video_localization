$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$py = Join-Path $root 'runtime\python312\python.exe'
& $py -m compileall -q (Join-Path $root 'src') (Join-Path $root 'tests')
if ($LASTEXITCODE -ne 0) { throw "Compilation check failed: $LASTEXITCODE" }
& $PSScriptRoot\run_tests.ps1
& $py -m pip check
if ($LASTEXITCODE -ne 0) { throw "Dependency check failed: $LASTEXITCODE" }
& $py -m go1loc.cli doctor --project-root $root --output (Join-Path $root 'results\doctor.json')
if ($LASTEXITCODE -ne 0) { throw "Doctor failed: $LASTEXITCODE" }
