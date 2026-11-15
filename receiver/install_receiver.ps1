# Install the Team A2A receiver for the CURRENT Windows user.
# Run in PowerShell AS THAT USER (not as another admin account), from this folder:
#     powershell -ExecutionPolicy Bypass -File .\install_receiver.ps1
# Optional: -NoSleep  turns off sleep on AC power (needs an elevated PowerShell).
param([switch]$NoSleep)

$ErrorActionPreference = 'Stop'
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$script = Join-Path $here 'receiver.py'
$cfg = Join-Path $here 'receiver.json'
$taskName = 'TeamA2A Receiver'

if (-not (Test-Path $cfg)) { throw "receiver.json is missing. Make it on the hub: python hub.py add-pc $env:COMPUTERNAME" }
$pyw = (Get-Command pythonw -ErrorAction SilentlyContinue).Source
if (-not $pyw) { throw 'pythonw was not found. Install Python 3.8+ and add it to PATH.' }
if (-not (Get-Command claude -ErrorAction SilentlyContinue)) { throw "'claude' was not found. Install Claude Code CLI and sign in (claude, then /login)." }

$user = "$env:USERDOMAIN\$env:USERNAME"
$action = New-ScheduledTaskAction -Execute $pyw -Argument "`"$script`"" -WorkingDirectory $here
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $user
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -StartWhenAvailable -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) `
    -ExecutionTimeLimit ([TimeSpan]::Zero) -MultipleInstances IgnoreNew
# Interactive = runs in the user's desktop session, so Claude can drive Revit/CAD while the PC is locked.
$principal = New-ScheduledTaskPrincipal -UserId $user -LogonType Interactive -RunLevel Limited

Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger -Settings $settings `
    -Principal $principal -Description 'Team A2A: runs Claude Code tasks from the office hub' -Force | Out-Null
Start-ScheduledTask -TaskName $taskName
Write-Host "Installed and started '$taskName' for $user."

if ($NoSleep) {
    powercfg /change standby-timeout-ac 0
    powercfg /change hibernate-timeout-ac 0
    Write-Host 'Sleep and hibernate on AC power are off.'
}
Write-Host 'Lock the PC at night (Win+L). Do not log off: the receiver stops when you log off.'
