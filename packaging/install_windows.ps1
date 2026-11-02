$ErrorActionPreference = 'Stop'
$source = Join-Path $PSScriptRoot 'Docs-Utilz.exe'
if (-not (Test-Path -LiteralPath $source)) { throw 'Keep this installer beside Docs-Utilz.exe.' }
$destination = Join-Path $env:LOCALAPPDATA 'Programs\Docs-Utilz'
$exe = Join-Path $destination 'Docs-Utilz.exe'
New-Item -ItemType Directory -Path $destination -Force | Out-Null
try { Copy-Item -LiteralPath $source -Destination $exe -Force }
catch { throw 'Close Docs-Utilz, then run Install.cmd again.' }
$startMenu = Join-Path $env:APPDATA 'Microsoft\Windows\Start Menu\Programs'
$shell = New-Object -ComObject WScript.Shell
foreach ($directory in @($startMenu, [Environment]::GetFolderPath('Desktop'))) {
    $shortcut = $shell.CreateShortcut((Join-Path $directory 'Docs-Utilz.lnk'))
    $shortcut.TargetPath = $exe
    $shortcut.WorkingDirectory = $destination
    $shortcut.IconLocation = "$exe,0"
    $shortcut.Save()
}
Copy-Item -LiteralPath (Join-Path $PSScriptRoot 'uninstall_windows.ps1') -Destination $destination -Force
$key = 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall\Docs-Utilz'
New-Item -Path $key -Force | Out-Null
$uninstall = 'powershell.exe -NoProfile -ExecutionPolicy Bypass -File "' + (Join-Path $destination 'uninstall_windows.ps1') + '"'
$properties = @{
    DisplayName = 'Docs-Utilz'
    DisplayVersion = '2.1.1'
    InstallLocation = $destination
    DisplayIcon = "$exe,0"
    UninstallString = $uninstall
}
foreach ($property in $properties.GetEnumerator()) {
    New-ItemProperty -Path $key -Name $property.Key -Value $property.Value -PropertyType String -Force | Out-Null
}
New-ItemProperty -Path $key -Name NoModify -Value 1 -PropertyType DWord -Force | Out-Null
New-ItemProperty -Path $key -Name NoRepair -Value 1 -PropertyType DWord -Force | Out-Null
Write-Host "Installed Docs-Utilz to $destination"
Write-Host 'Open it from the Desktop or Start menu. No Python installation is needed.'
Write-Host 'To update, close the app and run the new Install.cmd. Your saved settings are preserved.'
