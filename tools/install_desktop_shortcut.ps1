param(
    [Parameter(Mandatory=$true)][string]$MapDirectory,
    [Parameter(Mandatory=$true)][string]$SidecarDirectory,
    [string]$SnapshotDirectory = '',
    [int]$Port = 8767
)
$ErrorActionPreference = 'Stop'
$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$pythonConsole = Join-Path $projectRoot 'runtime\python312\python.exe'
$pythonWindowless = Join-Path $projectRoot 'runtime\python312\pythonw.exe'
if (!(Test-Path -LiteralPath $pythonConsole) -or !(Test-Path -LiteralPath $pythonWindowless)) {
    throw 'Existing project Python runtime not found. Nothing will be installed.'
}
$launchConfig = Join-Path $projectRoot '.cache\desktop_viewer.json'
$configureArgs = @('-m', 'go1loc.desktop', '--config', $launchConfig, '--configure',
    '--map-dir', $MapDirectory, '--sidecar-dir', $SidecarDirectory, '--port', "$Port")
if ($SnapshotDirectory) { $configureArgs += @('--snapshot-dir', $SnapshotDirectory) }
Push-Location $projectRoot
try {
    & $pythonConsole @configureArgs
    if ($LASTEXITCODE -ne 0) { throw "Invalid configuration. See $launchConfig (matching .log file)." }
} finally { Pop-Location }
# ASCII source remains usable in Windows PowerShell 5.1 as well as PowerShell 7.
$shortcutLabel = 'Go1 ' + (-join ([char[]]@(0x5730,0x56fe,0x4e0e,0x7167,0x7247,0x5b9a,0x4f4d)))
$desktopFolder = [Environment]::GetFolderPath('Desktop')
$shortcutPath = Join-Path $desktopFolder ($shortcutLabel + '.lnk')
$shellObject = New-Object -ComObject WScript.Shell
if (Test-Path -LiteralPath $shortcutPath) {
    $previous = $shellObject.CreateShortcut($shortcutPath)
    if ($previous.TargetPath -ne $pythonWindowless -or $previous.Arguments -notlike '*go1loc.desktop*') {
        throw "Existing unrelated shortcut left untouched: $shortcutPath"
    }
}
$shortcut = $shellObject.CreateShortcut($shortcutPath)
$shortcut.TargetPath = $pythonWindowless
$shortcut.Arguments = '-m go1loc.desktop --config "' + $launchConfig + '"'
$shortcut.WorkingDirectory = $projectRoot
$shortcut.Description = 'Go1 offline map and photo localization. No robot connection or motion.'
$shortcut.WindowStyle = 7
$shortcut.IconLocation = "$env:SystemRoot\System32\shell32.dll,13"
$shortcut.Save()
$verified = $shellObject.CreateShortcut($shortcutPath)
if ($verified.TargetPath -ne $pythonWindowless -or $verified.WorkingDirectory -ne $projectRoot) {
    throw 'Shortcut verification failed.'
}
Write-Output "Shortcut: $shortcutPath"
Write-Output "Config: $launchConfig"
