param(
    [string]$StartMenuRoot = ""
)

$ErrorActionPreference = "Stop"
$repositoryRoot = Split-Path -Parent $PSScriptRoot
$launcher = Join-Path $PSScriptRoot "Galaxy-USB.ps1"
if (-not (Test-Path -LiteralPath $launcher -PathType Leaf)) {
    throw "Galaxy-USB.ps1 was not found next to this installer."
}

if ([string]::IsNullOrWhiteSpace($StartMenuRoot)) {
    $StartMenuRoot = Join-Path $env:APPDATA "Microsoft\Windows\Start Menu\Programs"
}
$shortcutDirectory = Join-Path $StartMenuRoot "Galaxy USB"
New-Item -ItemType Directory -Path $shortcutDirectory -Force | Out-Null

$powerShellPath = (Get-Process -Id $PID).Path
$shell = New-Object -ComObject WScript.Shell
$entries = @(
    [PSCustomObject]@{ Name = "Galaxy USB"; Mode = ""; Description = "Choose a Galaxy USB display mode" },
    [PSCustomObject]@{ Name = "Mirror (ADB) - Galaxy USB"; Mode = "mirror"; Description = "Mirror and control Android over USB with ADB" },
    [PSCustomObject]@{ Name = "Mirror (MediaProjection) - Galaxy USB"; Mode = "projection"; Description = "View Android over USB AOA without USB debugging" },
    [PSCustomObject]@{ Name = "Desktop (ADB) - Galaxy USB"; Mode = "desktop"; Description = "Open an Android virtual display over USB with ADB" }
)

foreach ($entry in $entries) {
    $shortcutPath = Join-Path $shortcutDirectory ($entry.Name + ".lnk")
    $shortcut = $shell.CreateShortcut($shortcutPath)
    $shortcut.TargetPath = $powerShellPath
    $quotedLauncher = '"' + $launcher + '"'
    $shortcut.Arguments = "-NoLogo -NoProfile -ExecutionPolicy Bypass -File $quotedLauncher -KeepOpen"
    if ($entry.Mode) {
        $shortcut.Arguments += " -Mode $($entry.Mode)"
    }
    $shortcut.WorkingDirectory = $repositoryRoot
    $shortcut.Description = $entry.Description
    $shortcut.WindowStyle = 1
    $shortcut.Save()
}

Write-Host "Installed Galaxy USB shortcuts in: $shortcutDirectory"
