param([switch]$VerifyOnly)
$ErrorActionPreference = 'Stop'
$workbenchRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
$workbenchPython = Join-Path $workbenchRoot 'runtime\python312\pythonw.exe'
$workbenchConfig = Join-Path $workbenchRoot 'data\workbench\launcher.json'
if (!(Test-Path -LiteralPath $workbenchPython) -or !(Test-Path -LiteralPath $workbenchConfig)) {
    throw 'Missing project Python or data/workbench/launcher.json; see unified-workbench guide.'
}
$workbenchPath = Join-Path ([Environment]::GetFolderPath('Desktop')) 'Go1 采集建图定位工作台.lnk'
$workbenchShell = New-Object -ComObject WScript.Shell
$workbenchShortcut = $workbenchShell.CreateShortcut($workbenchPath)
$workbenchArguments = '-m go1loc.workbench --config "' + $workbenchConfig + '"'
if (!$VerifyOnly) {
    if ((Test-Path -LiteralPath $workbenchPath) -and
        ($workbenchShortcut.TargetPath -ne $workbenchPython -or $workbenchShortcut.Arguments -ne $workbenchArguments)) {
        throw 'An unrelated shortcut already exists; not overwritten.'
    }
    $workbenchShortcut.TargetPath = $workbenchPython
    $workbenchShortcut.Arguments = $workbenchArguments
    $workbenchShortcut.WorkingDirectory = $workbenchRoot
    $workbenchShortcut.Description = 'Go1 摄像头录制、建图、实时视觉定位和方向引导；不自动连接，不控制运动。'
    $workbenchShortcut.IconLocation = $workbenchPython + ',0'
    $workbenchShortcut.WindowStyle = 7
    $workbenchShortcut.Save()
}
if (!(Test-Path -LiteralPath $workbenchPath) -or $workbenchShortcut.TargetPath -ne $workbenchPython -or
    $workbenchShortcut.Arguments -ne $workbenchArguments -or $workbenchShortcut.WorkingDirectory -ne $workbenchRoot) {
    throw 'Shortcut verification failed.'
}
[PSCustomObject]@{Path=$workbenchPath; TargetPath=$workbenchShortcut.TargetPath; Verified=$true}
