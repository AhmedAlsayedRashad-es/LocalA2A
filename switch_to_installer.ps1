# Switch this PC from the hand-made setup (old D: folder) to the installed hub.
# Keeps your owner token, the join code and the PCs. Run it yourself, in PowerShell:
#     powershell -ExecutionPolicy Bypass -File H:\A2ALocal\switch_to_installer.ps1
$ErrorActionPreference = 'Stop'
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$hubDir = Join-Path $env:LOCALAPPDATA 'TeamA2A-Hub'
$dataDir = Join-Path $hubDir 'data'

Write-Host '1. Stopping the old hub and receiver ...'
foreach ($t in 'TeamA2A Hub', 'TeamA2A Receiver') {
    Stop-ScheduledTask -TaskName $t -ErrorAction SilentlyContinue
    Unregister-ScheduledTask -TaskName $t -Confirm:$false -ErrorAction SilentlyContinue
}
Get-CimInstance Win32_Process -Filter "Name like 'python%' or Name='TeamA2A-Hub.exe'" |
    Where-Object { $_.Name -eq 'TeamA2A-Hub.exe' -or $_.CommandLine -match 'hub\.py|receiver\.py' } |
    ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
Start-Sleep 2

Write-Host '2. Copying the hub settings and data ...'
New-Item -ItemType Directory -Force $dataDir | Out-Null
$cfg = Get-Content (Join-Path $here 'hub\hub.json') -Raw | ConvertFrom-Json
$oldData = $cfg.data_dir
if ($oldData -and (Test-Path $oldData) -and -not (Test-Path (Join-Path $dataDir 'hub.db'))) {
    Copy-Item (Join-Path $oldData '*') $dataDir -Recurse -Force
}
$cfg.data_dir = $dataDir
$json = $cfg | ConvertTo-Json -Depth 10
[System.IO.File]::WriteAllText((Join-Path $hubDir 'hub.json'), $json, (New-Object System.Text.UTF8Encoding $false))

Write-Host '3. Running TeamA2A-Hub-Setup (Windows asks for admin rights for the firewall) ...'
$owner = ($cfg.admins.PSObject.Properties | Select-Object -First 1).Name
$p = Start-Process (Join-Path $here 'dist\TeamA2A-Hub-Setup.exe') -ArgumentList '--silent', '--owner', $owner -PassThru -Wait
if ($p.ExitCode -ne 0) { throw "TeamA2A-Hub-Setup failed (exit $($p.ExitCode))." }

Start-Sleep 3
$h = Invoke-RestMethod http://127.0.0.1:8765/health -TimeoutSec 10
Write-Host "Hub: $($h.service) is running. Dashboard: http://localhost:8765/"
Write-Host 'Next: run H:\A2ALocal\dist\TeamA2A-Setup.exe to install the agent on this PC.'
