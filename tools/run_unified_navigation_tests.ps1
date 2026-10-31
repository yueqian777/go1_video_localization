$ErrorActionPreference = 'Stop'
$Root = Split-Path -Parent $PSScriptRoot
$Control = Join-Path $Root 'integrations\go1_remote_control'
$Python = Join-Path $Root 'runtime\python312\python.exe'
if (-not (Test-Path -LiteralPath $Python)) {
    $PythonCommand = Get-Command py -ErrorAction SilentlyContinue
    if ($PythonCommand) { $Python = $PythonCommand.Source }
    else { $Python = (Get-Command python -ErrorAction Stop).Source }
}
$MsysBash = 'C:\msys64\usr\bin\bash.exe'
if (-not (Test-Path -LiteralPath $MsysBash)) {
    $MsysBash = (Get-Command bash -ErrorAction Stop).Source
}
$env:PYTHONUTF8 = '1'
$env:PYTHONPATH = "$(Join-Path $Root 'src');$(Join-Path $Control 'pc');$(Join-Path $Control 'tests')"

Write-Host '== 编译离线接收端（GO1_SIM_ONLY） ==' -ForegroundColor Cyan
$Build = Join-Path $Control 'build'
New-Item -ItemType Directory -Force -Path $Build | Out-Null
$Binary = Join-Path $Build 'go1_receiver_sim.exe'
$UnixControl = '/' + $Control.Substring(0, 1).ToLower() + ($Control.Substring(2) -replace '\\', '/')
$CompileCommand = "export PATH=/mingw64/bin:/usr/bin:`$PATH; cd '$UnixControl'; g++ -std=c++14 -Wall -Wextra -Wpedantic -DGO1_SIM_ONLY robot/receiver.cpp -static -lws2_32 -o build/go1_receiver_sim.exe"
& $MsysBash -lc $CompileCommand
if ($LASTEXITCODE -ne 0) { throw '模拟接收端编译失败' }
$env:GO1_SIM_BINARY = $Binary

Write-Host '== 视觉闭环与 Runner 测试 ==' -ForegroundColor Cyan
Push-Location $Root
try {
    & $Python -m unittest discover -s tests -p 'test_navigation_runner.py'
    if ($LASTEXITCODE -ne 0) { throw 'Runner 测试失败' }
    & $Python -m unittest discover -s tests -p 'test_free_space_navigation.py'
    if ($LASTEXITCODE -ne 0) { throw '视觉闭环测试失败' }
}
finally { Pop-Location }

Write-Host '== 控制权限与跨仓库 HTTP 测试 ==' -ForegroundColor Cyan
Push-Location $Control
try {
    & $Python -m unittest discover -s tests -p 'test_cross_repo_navigation.py'
    if ($LASTEXITCODE -ne 0) { throw '跨仓库 HTTP 测试失败' }
    & $Python -m unittest discover -s tests -p 'test_navigation_authority.py'
    if ($LASTEXITCODE -ne 0) { throw '跨仓库控制测试失败' }
}
finally { Pop-Location }

Write-Host '验证通过：以上测试使用本机模拟接收端，不连接真实 Go1。' -ForegroundColor Green
