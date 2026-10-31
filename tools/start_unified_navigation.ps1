param(
    [ValidateSet('SHADOW', 'SIMULATION', 'TEST')]
    [string]$Mode = 'SHADOW'
)

$ErrorActionPreference = 'Stop'
$Root = Split-Path -Parent $PSScriptRoot
$Control = Join-Path $Root 'integrations\go1_remote_control'
$Python = Join-Path $Root 'runtime\python312\python.exe'
if (-not (Test-Path -LiteralPath $Python)) {
    $PythonCommand = Get-Command py -ErrorAction SilentlyContinue
    if ($PythonCommand) { $Python = $PythonCommand.Source }
    else { $Python = (Get-Command python -ErrorAction Stop).Source }
}

$MsysBin = 'C:\msys64\mingw64\bin'
if (Test-Path -LiteralPath $MsysBin) { $env:Path = "$MsysBin;$env:Path" }
$env:PYTHONUTF8 = '1'

switch ($Mode) {
    'SHADOW' {
        $env:PYTHONPATH = Join-Path $Root 'src'
        & $Python -m go1loc.navigation_runner --mode SHADOW
        exit $LASTEXITCODE
    }
    'SIMULATION' {
        $env:PYTHONPATH = "$(Join-Path $Control 'tools');$(Join-Path $Root 'src')"
        Push-Location $Control
        try { & $Python .\tools\run_demo.py }
        finally { Pop-Location }
        exit $LASTEXITCODE
    }
    'TEST' {
        & (Join-Path $PSScriptRoot 'run_unified_navigation_tests.ps1')
        exit $LASTEXITCODE
    }
}
