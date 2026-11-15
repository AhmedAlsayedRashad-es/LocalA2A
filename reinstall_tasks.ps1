# Re-install the hub and receiver autostart tasks from THIS folder (use after you move the project).
#     powershell -ExecutionPolicy Bypass -File .\reinstall_tasks.ps1
$ErrorActionPreference = 'Stop'
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
& (Join-Path $here 'hub\uninstall_hub.ps1')
& (Join-Path $here 'receiver\uninstall_receiver.ps1')
Start-Sleep -Seconds 2
& (Join-Path $here 'hub\install_hub.ps1')
Start-Sleep -Seconds 3
& (Join-Path $here 'receiver\install_receiver.ps1')
Get-ScheduledTask -TaskName 'TeamA2A*' | ForEach-Object { "{0}: {1}  {2}" -f $_.TaskName, $_.State, $_.Actions.Arguments }
