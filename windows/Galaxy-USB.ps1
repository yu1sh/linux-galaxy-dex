param(
    [Parameter(Position = 0)]
    [string]$Mode = "",
    [switch]$KeepOpen
)

$ErrorActionPreference = "Continue"
$script:RepositoryRoot = Split-Path -Parent $PSScriptRoot

function Stop-WithError {
    param(
        [string]$Message,
        [int]$ExitCode = 1
    )

    [Console]::Error.WriteLine("Galaxy USB: $Message")
    if ($KeepOpen) {
        Read-Host "Press Enter to close this window" | Out-Null
    }
    exit $ExitCode
}

function Write-Usage {
    @"
Usage: Galaxy-USB.ps1 [mirror|projection|desktop|list|help]

  mirror      Mirror (ADB) - Galaxy USB; requires USB debugging
  projection  Mirror (MediaProjection) - Galaxy USB; USB AOA, no ADB/debugging
  desktop     Desktop (ADB) - Galaxy USB; requires USB debugging
  list        Show ADB devices connected over USB

Run without a mode to choose from an interactive menu. The Start menu installer
creates a selector shortcut and one shortcut for each display mode.
"@
}

function Select-Mode {
    Write-Host ""
    Write-Host "Galaxy USB"
    Write-Host "  1. Mirror (ADB)"
    Write-Host "  2. Mirror (MediaProjection)"
    Write-Host "  3. Desktop (ADB)"
    Write-Host "  4. List ADB devices"
    Write-Host "  Q. Quit"
    $choice = (Read-Host "Select a mode").Trim()
    switch ($choice) {
        "1" { return "mirror" }
        "2" { return "projection" }
        "3" { return "desktop" }
        "4" { return "list" }
        { $_ -match "^[qQ]$" } { return "" }
        default { Stop-WithError "Invalid selection: $choice" 2 }
    }
}

function Get-Setting {
    param(
        [string]$Name,
        [string]$Default
    )

    $value = [Environment]::GetEnvironmentVariable($Name)
    if ([string]::IsNullOrWhiteSpace($value)) {
        return $Default
    }
    return $value
}

function Resolve-Application {
    param(
        [string]$Name,
        [string]$Purpose
    )

    $command = Get-Command -Name $Name -CommandType Application -ErrorAction SilentlyContinue |
        Select-Object -First 1
    if ($null -eq $command) {
        Stop-WithError "$Purpose ($Name) が見つかりません。インストールしてPATHを確認してください。"
    }
    if ($command.Source) {
        return $command.Source
    }
    return $command.Path
}

function Resolve-Python {
    $launcher = Get-Command -Name "py.exe" -CommandType Application -ErrorAction SilentlyContinue |
        Select-Object -First 1
    if ($null -ne $launcher) {
        return @{
            Path = $launcher.Source
            Prefix = @("-3")
        }
    }

    $python = Get-Command -Name "python.exe" -CommandType Application -ErrorAction SilentlyContinue |
        Select-Object -First 1
    if ($null -ne $python) {
        return @{
            Path = $python.Source
            Prefix = @()
        }
    }
    Stop-WithError "Python 3.10以降が見つかりません。PythonをインストールしてPATHを確認してください。"
}

function Get-AdbUsbDevices {
    param([string]$AdbPath)

    $lines = @(& $AdbPath devices -l 2>$null)
    if ($LASTEXITCODE -ne 0) {
        Stop-WithError "adb devices を実行できませんでした。ADBのUSBドライバーを確認してください。"
    }

    foreach ($line in $lines) {
        $text = ([string]$line).Trim()
        if ([string]::IsNullOrWhiteSpace($text)) {
            continue
        }
        $fields = $text -split "\s+"
        if ($fields.Count -lt 2 -or $fields[0] -eq "List") {
            continue
        }
        [PSCustomObject]@{
            Serial = $fields[0]
            State = $fields[1]
            IsUsb = ($text -match "(^|\s)usb:")
        }
    }
}

function Wait-ForAdbUsbDevice {
    param(
        [string]$AdbPath,
        [int]$WaitSeconds
    )

    $requestedSerial = $env:GALAXY_ADB_SERIAL
    $lastUnauthorized = @()
    for ($elapsed = 0; $elapsed -le $WaitSeconds; $elapsed++) {
        $devices = @(Get-AdbUsbDevices -AdbPath $AdbPath)
        if ($requestedSerial) {
            $serialMatches = @($devices | Where-Object {
                $_.Serial -eq $requestedSerial -and $_.IsUsb
            })
            if (@($serialMatches | Where-Object { $_.State -eq "unauthorized" }).Count -gt 0) {
                Stop-WithError "端末の「USBデバッグを許可しますか？」でこのPCを許可してください。"
            }
            $authorized = @($serialMatches | Where-Object { $_.State -eq "device" })
            if ($authorized.Count -gt 0) {
                return $requestedSerial
            }
        }
        else {
            $lastUnauthorized = @($devices | Where-Object {
                $_.State -eq "unauthorized" -and $_.IsUsb
            })
            $authorized = @($devices | Where-Object {
                $_.State -eq "device" -and $_.IsUsb
            })
            if ($authorized.Count -gt 1) {
                Stop-WithError "USB端末が複数あります。GALAXY_ADB_SERIAL=端末ID を指定してください。"
            }
            if ($authorized.Count -eq 1) {
                return $authorized[0].Serial
            }
        }

        if ($elapsed -lt $WaitSeconds) {
            Start-Sleep -Seconds 1
        }
    }

    if ($lastUnauthorized.Count -gt 0) {
        Stop-WithError "端末の「USBデバッグを許可しますか？」でこのPCを許可してください。"
    }
    if ($requestedSerial) {
        Stop-WithError "指定した端末IDがUSB接続の認証済み端末として見つかりません。GALAXY_ADB_SERIALを確認してください。"
    }
    Stop-WithError "認証済みのUSB端末が見つかりません。データ通信対応USBケーブルとUSBデバッグを確認してください。"
}

if ([string]::IsNullOrWhiteSpace($Mode)) {
    $Mode = Select-Mode
}
if ([string]::IsNullOrWhiteSpace($Mode)) {
    exit 0
}
$Mode = $Mode.Trim().ToLowerInvariant()
if ($Mode -in @("help", "-h", "--help")) {
    Write-Usage
    exit 0
}
if ($Mode -notin @("mirror", "projection", "desktop", "list")) {
    Write-Usage
    Stop-WithError "Unknown mode: $Mode" 2
}

if ($Mode -eq "projection") {
    $receiver = Join-Path $script:RepositoryRoot "receiver\gusb_receiver.py"
    if (-not (Test-Path -LiteralPath $receiver -PathType Leaf)) {
        Stop-WithError "receiver\gusb_receiver.py が見つかりません。リポジトリを展開し直してください。"
    }
    $python = Resolve-Python
    $ffplay = Resolve-Application -Name "ffplay.exe" -Purpose "ffplay (FFmpeg)"
    $projectionWait = Get-Setting -Name "GALAXY_PROJECTION_WAIT_SECONDS" -Default "120"
    $pythonArgs = @($python.Prefix) + @(
        $receiver,
        "--wait", $projectionWait,
        "--hello-timeout", $projectionWait,
        "--ffplay", $ffplay,
        "--window-title", "Mirror (MediaProjection) - Galaxy USB"
    )
    if ($env:GALAXY_PROJECTION_DEVICE) {
        $pythonArgs += @("--device", $env:GALAXY_PROJECTION_DEVICE)
    }
    Write-Host "Galaxy USB: AOA over USB is starting. USB debugging is not used."
    & $python.Path @pythonArgs
    $receiverStatus = $LASTEXITCODE
    if ($receiverStatus -ne 0) {
        [Console]::Error.WriteLine("Galaxy USB: MediaProjection stopped. Check the USB cable, WinUSB driver, and Android sharing/accessory prompts.")
        if ($KeepOpen) {
            Read-Host "Press Enter to close this window" | Out-Null
        }
    }
    exit $receiverStatus
}

$adb = Resolve-Application -Name "adb.exe" -Purpose "Android platform tools (adb)"
if ($Mode -ne "list") {
    $scrcpy = Resolve-Application -Name "scrcpy.exe" -Purpose "scrcpy 4.0 or later"
}

& $adb start-server *> $null
if ($LASTEXITCODE -ne 0) {
    Stop-WithError "ADBを起動できません。USBドライバーとUSBデバッグ設定を確認してください。"
}
if ($Mode -eq "list") {
    & $adb devices -l
    if ($LASTEXITCODE -ne 0) {
        Stop-WithError "adb devices を実行できませんでした。"
    }
    exit $LASTEXITCODE
}

$waitText = Get-Setting -Name "GALAXY_WAIT_SECONDS" -Default "15"
[int]$waitSeconds = 0
if (-not [int]::TryParse($waitText, [ref]$waitSeconds) -or $waitSeconds -lt 0) {
    Stop-WithError "GALAXY_WAIT_SECONDSには0以上の整数を指定してください。" 2
}
$serial = Wait-ForAdbUsbDevice -AdbPath $adb -WaitSeconds $waitSeconds
$modelOutput = & $adb -s $serial shell getprop ro.product.model 2>$null |
    Select-Object -First 1
$model = ([string]$modelOutput).Trim()
if ([string]::IsNullOrWhiteSpace($model)) {
    $model = "Android device"
}

$commonArgs = @(
    "--serial=$serial",
    "--video-codec=h264",
    "--video-bit-rate=$(Get-Setting -Name 'GALAXY_BITRATE' -Default '16M')",
    "--max-fps=$(Get-Setting -Name 'GALAXY_MAX_FPS' -Default '60')",
    "--video-buffer=0",
    "--keyboard=$(Get-Setting -Name 'GALAXY_KEYBOARD' -Default 'uhid')",
    "--mouse=$(Get-Setting -Name 'GALAXY_MOUSE' -Default 'uhid')",
    "--keep-active"
)

if ($Mode -eq "mirror") {
    $maxSize = Get-Setting -Name "GALAXY_MAX_SIZE" -Default "1920"
    $scrcpyArgs = @($commonArgs) + @(
        "--max-size=$maxSize",
        "--window-title=Mirror (ADB) - Galaxy USB"
    )
    Write-Host "Galaxy USB: Starting Mirror (ADB) for $model."
}
else {
    $desktopSize = Get-Setting -Name "GALAXY_DESKTOP_SIZE" -Default "1920x1080/320"
    $desktopApp = Get-Setting -Name "GALAXY_DESKTOP_APP" -Default "none"
    $scrcpyArgs = @($commonArgs) + @(
        "--new-display=$desktopSize",
        "--flex-display",
        "--no-vd-destroy-content",
        "--display-ime-policy=local",
        "--window-title=Desktop (ADB) - Galaxy USB"
    )
    if ($desktopApp -and $desktopApp -ne "none") {
        & $adb -s $serial shell pm path $desktopApp *> $null
        if ($LASTEXITCODE -eq 0) {
            $scrcpyArgs += "--start-app=$desktopApp"
        }
    }
    Write-Host "Galaxy USB: Starting Desktop (ADB) for $model."
}

& $scrcpy @scrcpyArgs
$scrcpyStatus = $LASTEXITCODE
if ($scrcpyStatus -ne 0 -and $KeepOpen) {
    Read-Host "Press Enter to close this window" | Out-Null
}
exit $scrcpyStatus
