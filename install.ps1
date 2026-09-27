# 12VHPWR Guard - Portable Installer
$ErrorActionPreference = "Stop"

function Write-Info($msg) { Write-Host "[INFO]  $msg" -ForegroundColor Cyan }
function Write-Warn($msg) { Write-Host "[WARN]  $msg" -ForegroundColor Yellow }
function Write-Err($msg)  { Write-Host "[ERROR] $msg" -ForegroundColor Red }

$IsAdmin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole(
    [Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $IsAdmin) {
    Write-Err "Administrator rights required (Task Scheduler / Event Log registration)."
    Write-Err "Right-click install.bat and choose 'Run as administrator'."
    exit 1
}

$BaseDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$ScriptName = "hwinfo_12vhpwr_guard.py"
$ScriptPath = Join-Path $BaseDir $ScriptName
$VenvDir = Join-Path $BaseDir "venv"
$PyExe = Join-Path $VenvDir "Scripts\python.exe"
$PywExe = Join-Path $VenvDir "Scripts\pythonw.exe"
$ReqPath = Join-Path $BaseDir "requirements.txt"
$ConfigPath = Join-Path $BaseDir "config.ini"
$LogsDir = Join-Path $BaseDir "logs"

$TaskName = "12VHPWR Guard"
$EventSource = "12VHPWR Guard"

if (!(Test-Path $ScriptPath)) { Write-Err "Missing script."; exit 1 }

# Well-known SIDs rather than names, because "Users" and "Administrators" are
# translated on non-English Windows.
$SidSystem = "S-1-5-18"
$SidAdmins = "S-1-5-32-544"
$SidUsers  = "S-1-5-32-545"

# True for anything under C:\Users (or wherever profiles live on this PC).
$ProfilesRoot = (Split-Path -Parent $env:USERPROFILE).TrimEnd('\') + '\'
function Test-InUserProfile($path) {
    return ($path.TrimEnd('\') + '\').StartsWith($ProfilesRoot, [StringComparison]::OrdinalIgnoreCase)
}

# All-users mode registers the task for the built-in Users group, so it starts in the
# session of whoever logs on and the tray icon appears on their desktop. A re-run
# defaults to whatever the existing task already uses, so an upgrade from another
# account does not quietly switch the PC back to one user.
$Existing = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
$WasAllUsers = [bool]($Existing -and $Existing.Principal.GroupId)
$Hint = if ($WasAllUsers) { "Y/n" } else { "y/N" }
$Answer = Read-Host "Start 12VHPWR Guard for every Windows account on this PC? ($Hint)"
$AllUsers = if ($Answer -match '^(y|yes)$') { $true } elseif ($Answer -match '^(n|no)$') { $false } else { $WasAllUsers }

if ($AllUsers) {
    # Other accounts cannot read inside someone else's profile, so the task would start
    # for them and fail on the first file it opens.
    if (Test-InUserProfile $BaseDir) {
        Write-Err "The guard is in a user profile folder ($BaseDir), which other accounts cannot read."
        Write-Err "Move the folder somewhere like C:\12vhpwr_guard and run the installer again."
        exit 1
    }
}

# Functional check: Get-Command also matches the Microsoft Store alias stub, which
# then does nothing useful. Running the interpreter proves it exists and is new enough.
# Done before anything is created so a failed check leaves no litter behind.
& python -c "import sys; sys.exit(0 if sys.version_info >= (3,10) else 1)" 2>$null
if ($LASTEXITCODE -ne 0) {
    Write-Err "Python 3.10+ not found on PATH."
    Write-Err "Install from https://www.python.org/downloads/ and tick 'Add python.exe to PATH'."
    exit 1
}

if ($AllUsers) {
    # python.org's default "Install Now" puts Python inside the installing user's
    # profile, and a venv only points back at that interpreter, so every other account
    # would fail to start it. Checked before anything is created.
    $PyForVenv = if (Test-Path $PyExe) { $PyExe } else { "python" }
    $BasePrefix = (& $PyForVenv -c "import sys; print(sys.base_prefix)" 2>$null | Out-String).Trim()
    if ($BasePrefix -and (Test-InUserProfile $BasePrefix)) {
        Write-Err "Python is installed for your account only ($BasePrefix), so other accounts cannot run it."
        Write-Err "Reinstall Python with 'Install Python for all users' ticked (Customize installation),"
        Write-Err "delete the venv folder here, then run the installer again."
        exit 1
    }
}

if (!(Test-Path $LogsDir)) { New-Item -ItemType Directory -Path $LogsDir | Out-Null }

if (!(Test-Path $ConfigPath)) {
@"
[Settings]
threshold_amps = 9.5
sustained_seconds_required = 15
sensor_backend = auto
response_mode = tiered
"@ | Set-Content -Path $ConfigPath -Encoding UTF8
}

if (!(Test-Path $PyExe)) {
    & python -m venv $VenvDir
}

& $PyExe -m pip install --upgrade pip
& $PyExe -m pip install -r $ReqPath

try { & $PyExe -m pywin32_postinstall -install } catch {}

try {
    if (-not [System.Diagnostics.EventLog]::SourceExists($EventSource)) {
        New-EventLog -LogName Application -Source $EventSource
    }
} catch {}

# The task runs this folder's code with highest privileges whenever an administrator
# logs on, so no other account may be able to change it. Folders directly under C:\
# inherit Modify for every signed-in user, and Python also imports from the script's
# own folder, so inheritance is cut and only Administrators and SYSTEM may write.
# The installing account (an administrator, or this script would have stopped) keeps
# Modify in both modes, so it can upgrade with git pull or by extracting a release
# without an elevated window. In all-users mode, standard accounts get write access to
# logs and config.ini only, which is all a guard without admin rights ever writes.
$Grants = @("*${SidSystem}:(OI)(CI)F", "*${SidAdmins}:(OI)(CI)F", "*${SidUsers}:(OI)(CI)RX",
    "*$([Security.Principal.WindowsIdentity]::GetCurrent().User.Value):(OI)(CI)M")
& icacls $BaseDir /inheritance:r /grant:r @Grants /Q | Out-Null
if ($LASTEXITCODE -ne 0) { Write-Warn "Could not set folder permissions on $BaseDir." }
if ($AllUsers) {
    & icacls $LogsDir /grant "*${SidUsers}:(OI)(CI)M" /Q | Out-Null
    & icacls $ConfigPath /grant "*${SidUsers}:M" /Q | Out-Null
}

# Upgrades: a still-running guard would keep the OLD code alive and hold the
# single-instance mutex against the new one, so the upgrade would not take effect
# until the next reboot. Stop it first; on a fresh install this is a no-op.
if ($Existing) {
    Write-Info "Existing install found - stopping the running guard to upgrade it."
    try { Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue } catch {}
}

try {
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue
} catch {}

$UserId = "$env:USERDOMAIN\$env:USERNAME"
$Action = New-ScheduledTaskAction -Execute $PywExe -Argument "`"$ScriptPath`"" -WorkingDirectory $BaseDir
if ($AllUsers) {
    # A group principal runs the task as the account that just logged on, in its own
    # session. Administrators get the elevated token; standard accounts run limited,
    # which means monitoring and emergency shutdown but no GPU clock or power limit.
    $Trigger = New-ScheduledTaskTrigger -AtLogOn
    $Principal = New-ScheduledTaskPrincipal -GroupId $SidUsers -RunLevel Highest
} else {
    $Trigger = New-ScheduledTaskTrigger -AtLogOn -User $UserId
    $Principal = New-ScheduledTaskPrincipal -UserId $UserId -LogonType Interactive -RunLevel Highest
}
# IgnoreNew also covers a second account logging on while the first is still signed
# in: the guard already running keeps protecting the card, and the guard's global
# single-instance mutex backs this up for a copy started by hand.
# RestartCount is deliberately high: a watchdog that gives up after a few crashes
# stops protecting the card exactly when something is already going wrong.
$Settings = New-ScheduledTaskSettingsSet -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -MultipleInstances IgnoreNew

# Without this the task defaults to a 72-hour limit and Task Scheduler kills the guard
# after 3 days of uptime. A time-limit stop is not a "failure", so RestartCount never fires.
$Settings.ExecutionTimeLimit = "PT0S"

Register-ScheduledTask -TaskName $TaskName -Action $Action -Trigger $Trigger -Principal $Principal -Settings $Settings | Out-Null

$startNow = Read-Host "Start 12VHPWR Guard now? (y/N)"
if ($startNow -match '^(y|yes)$') {
    Start-ScheduledTask -TaskName $TaskName
}

Write-Info "Install complete."
