$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$py = Join-Path $root 'runtime\python312\python.exe'
& $py -m compileall -q (Join-Path $root 'src') (Join-Path $root 'tests')
& $PSScriptRoot\run_tests.ps1
& (Join-Path $root 'runtime\python312\Scripts\go1loc.exe') doctor --project-root $root --output (Join-Path $root 'results\doctor.json')
