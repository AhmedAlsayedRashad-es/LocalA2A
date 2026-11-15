# Start the Team A2A hub at Windows login for the current user (hidden, restarts if it stops).
#     powershell -ExecutionPolicy Bypass -File .\install_hub.ps1
# Other PCs also need the port open (elevated PowerShell, once):
#     New-NetFirewallRule -DisplayName TeamA2A -Direction Inbound -LocalPort 8765 -Protocol TCP -Action Allow
$ErrorActionPreference = 'Stop'
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$taskName = 'TeamA2A Hub'
$pyw = (Get-Command pythonw -ErrorAction SilentlyContinue).Source
if (-not $pyw) { throw 'pythonw was not found. Install Python 3.8+ and add it to PATH.' }
$user = "$env:USERDOMAIN\$env:USERNAME"
$action = New-ScheduledTaskAction -Execute $pyw -Argument "`"$(Join-Path $here 'hub.py')`"" -WorkingDirectory $here
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $user
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -StartWhenAvailable -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) `
    -ExecutionTimeLimit ([TimeSpan]::Zero) -MultipleInstances IgnoreNew
$principal = New-ScheduledTaskPrincipal -UserId $user -LogonType Interactive -RunLevel Limited
Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger -Settings $settings `
    -Principal $principal -Description 'Team A2A hub' -Force | Out-Null
Start-ScheduledTask -TaskName $taskName
Write-Host "Installed and started '$taskName' for $user. Dashboard: http://localhost:8765/"
