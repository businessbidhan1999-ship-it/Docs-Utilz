$ErrorActionPreference = 'Stop'
$exe = Join-Path $PSScriptRoot 'Docs-Utilz.exe'
if (Test-Path -LiteralPath $exe) {
    try { Remove-Item -LiteralPath $exe }
    catch { throw 'Close Docs-Utilz and try uninstalling again.' }
}
$startMenu = Join-Path $env:APPDATA 'Microsoft\Windows\Start Menu\Programs'
foreach ($directory in @($startMenu, [Environment]::GetFolderPath('Desktop'))) {
    $shortcut = Join-Path $directory 'Docs-Utilz.lnk'
    if (Test-Path -LiteralPath $shortcut) { Remove-Item -LiteralPath $shortcut }
}
$key = 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall\Docs-Utilz'
if (Test-Path $key) { Remove-Item -LiteralPath $key }
Write-Host 'Application removed. Saved settings and documents were preserved.'
# Keep this script and the folder so uninstall never deletes user documents.
