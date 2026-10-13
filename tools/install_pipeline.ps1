$ErrorActionPreference = 'Stop'

$projectRoot = Split-Path -Parent $PSScriptRoot
$python = Join-Path $projectRoot 'runtime\python312\python.exe'
$cache = Join-Path $projectRoot '.cache\pip'
$hloc = Join-Path $projectRoot '.upstream\Hierarchical-Localization'
$lightGlue = Join-Path $projectRoot '.upstream\LightGlue'

if (-not (Test-Path -LiteralPath $python)) { throw "Missing D-drive Python runtime: $python" }
if (-not (Test-Path -LiteralPath $hloc)) { throw "Missing pinned hloc checkout: $hloc" }
if (-not (Test-Path -LiteralPath $lightGlue)) { throw "Missing pinned LightGlue checkout: $lightGlue" }

$env:PIP_CACHE_DIR = $cache
& $python -m pip install --disable-pip-version-check --no-warn-script-location --cache-dir $cache -r (Join-Path $projectRoot 'requirements-pipeline.txt')
& $python -m pip install --disable-pip-version-check --no-warn-script-location --cache-dir $cache --no-deps -e $lightGlue
& $python -m pip install --disable-pip-version-check --no-warn-script-location --cache-dir $cache --no-deps -e $hloc
& $python -m pip check

Write-Output 'Pipeline dependencies installed from the pinned D-drive upstream checkouts.'
