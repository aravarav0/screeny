$project = Split-Path -Parent $MyInvocation.MyCommand.Path
$bat = Join-Path $project "launch_screeny.bat"
$desktop = [Environment]::GetFolderPath("Desktop")
$shortcutPath = Join-Path $desktop "Screeny.lnk"

$shell = New-Object -ComObject WScript.Shell
$shortcut = $shell.CreateShortcut($shortcutPath)
$shortcut.TargetPath = $bat
$shortcut.WorkingDirectory = $project
$shortcut.WindowStyle = 1
$shortcut.Description = "Open Screeny - local voice assistant"
$shortcut.Save()

Write-Host "Created shortcut: $shortcutPath"
