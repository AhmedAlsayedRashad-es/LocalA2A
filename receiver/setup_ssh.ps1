<#
Team A2A SSH: let hub admins run cmd.exe commands on this PC over SSH (key only, every command logged).
Run ONCE, elevated (IT), after the Team A2A agent is installed and approved on the hub.

  powershell -ExecutionPolicy Bypass -File setup_ssh.ps1 -User colleague `
      -ReceiverConfig C:\Users\colleague\AppData\Local\TeamA2A\receiver.json

Needs TeamA2A-SSH.exe (from dist\, built by build\build.py) next to this script, or -SshExe PATH.
Without the exe:  -Python C:\Python312\python.exe  (uses a2a_ssh.py next to this script, or -ReceiverDir)

What it does:
  1. Installs the Windows OpenSSH Server (if missing).
  2. %ProgramData%\TeamA2A\ssh (SYSTEM + Administrators write, Users read): a copy of TeamA2A-SSH.exe,
     ssh.json (PC name, hub URL, token) and teama2a-ssh.cmd, the forced command that logs and runs.
  3. sshd_config: key only, no passwords, no forwarding, no tty, AllowUsers <User>, and ONE keys file
     %ProgramData%\ssh\administrators_authorized_keys that only SYSTEM and Administrators can change.
  4. DefaultShell = cmd.exe. Firewall: port 22 only from the hub's ssh_allowed_cidr.
  5. Scheduled task "TeamA2A SSH key sync" (SYSTEM, at startup and every 15 min) writes the admins'
     keys from the hub. A PC the hub refuses gets an empty keys file.
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$User,
    [Parameter(Mandatory = $true)][string]$ReceiverConfig,
    [string]$SshExe = '',
    [string]$Python = '',
    [string]$ReceiverDir = ''
)
$ErrorActionPreference = 'Stop'

$principal = New-Object Security.Principal.WindowsPrincipal([Security.Principal.WindowsIdentity]::GetCurrent())
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    throw 'Run this script as Administrator (elevated).'
}
if ($User -notmatch '^[A-Za-z0-9._-]{1,64}$') { throw "Bad user name '$User'." }
if (-not (Test-Path $ReceiverConfig)) { throw "Not found: $ReceiverConfig" }
$rc = Get-Content -Raw -Encoding UTF8 $ReceiverConfig | ConvertFrom-Json
foreach ($k in 'name', 'hub_url', 'token') {
    if (-not $rc.$k) { throw "receiver.json has no '$k'." }
}

# SIDs, so this works on any Windows language: SYSTEM, Administrators, Users
$SYSTEM = '*S-1-5-18'; $ADMINS = '*S-1-5-32-544'; $USERS = '*S-1-5-32-545'

function Write-Utf8($path, $text) {
    [IO.File]::WriteAllText($path, $text, (New-Object Text.UTF8Encoding($false)))
}

# ---- 1. OpenSSH Server
$cap = Get-WindowsCapability -Online | Where-Object Name -like 'OpenSSH.Server*'
if ($cap -and $cap.State -ne 'Installed') {
    Write-Host 'Installing OpenSSH Server ...'
    Add-WindowsCapability -Online -Name $cap.Name | Out-Null
}
$sshd = Join-Path $env:WINDIR 'System32\OpenSSH\sshd.exe'
if (-not (Test-Path $sshd)) { $sshd = (Get-Command sshd -ErrorAction Stop).Source }
$sshDir = Join-Path $env:ProgramData 'ssh'
if (-not (Test-Path (Join-Path $sshDir 'ssh_host_ed25519_key'))) {
    # The first start makes the host keys and %ProgramData%\ssh.
    Start-Service sshd
    Stop-Service sshd
}

# ---- 2. Team A2A folder (only SYSTEM and Administrators can change it)
$dir = Join-Path $env:ProgramData 'TeamA2A\ssh'
New-Item -ItemType Directory -Force $dir | Out-Null
& icacls $dir /inheritance:r /grant:r "${SYSTEM}:(OI)(CI)F" "${ADMINS}:(OI)(CI)F" "${USERS}:(OI)(CI)RX" | Out-Null
if ($LASTEXITCODE -ne 0) { throw "icacls failed on $dir" }

if ($Python) {
    if (-not $ReceiverDir) { $ReceiverDir = $PSScriptRoot }
    if ($Python -like "$env:SystemDrive\Users\*") {
        Write-Warning "$Python is in a user profile: that user could change what the SYSTEM sync task runs. Use a Python installed for all users."
    }
    Copy-Item -Force (Join-Path $ReceiverDir 'a2a_ssh.py') $dir
    $exe = $Python
    $pre = @((Join-Path $dir 'a2a_ssh.py'))
    $launch = "`"$Python`" `"$(Join-Path $dir 'a2a_ssh.py')`""
} else {
    if (-not $SshExe) { $SshExe = Join-Path $PSScriptRoot 'TeamA2A-SSH.exe' }
    if (-not (Test-Path $SshExe)) { throw "Not found: $SshExe (give -SshExe, or -Python)" }
    Copy-Item -Force $SshExe (Join-Path $dir 'TeamA2A-SSH.exe')
    $exe = Join-Path $dir 'TeamA2A-SSH.exe'
    $pre = @()
    $launch = "`"$exe`""
}
$sshJson = Join-Path $dir 'ssh.json'
Write-Utf8 $sshJson (@{ name = $rc.name; hub_url = $rc.hub_url; token = $rc.token } | ConvertTo-Json)
$launcher = Join-Path $dir 'teama2a-ssh.cmd'
if ($launcher -notmatch '^[A-Za-z]:\\[A-Za-z0-9_.\\-]+$') {
    throw "The launcher path '$launcher' has spaces or other characters sshd cannot take."
}
Write-Utf8 $launcher ("@echo off`r`n" +
    "$launch --config `"$sshJson`" shell %* --log-dir `"%LOCALAPPDATA%\TeamA2A\logs`"`r`n" +
    "exit /b %ERRORLEVEL%`r`n")

function Invoke-Agent([string[]]$more) {
    $out = & $exe @($pre + @('--config', $sshJson) + $more)
    return @{ code = $LASTEXITCODE; out = ($out -join "`n") }
}

# ---- hub network (firewall + from= in every key line)
$r = Invoke-Agent @('hub-cidr')
if ($r.code -ne 0 -or -not $r.out) {
    throw "The hub gave no SSH network. On the hub run: python hub.py ssh-cidr 192.168.1.0/24 (and approve this PC)."
}
$cidr = $r.out.Trim()
Write-Host "Hub allows SSH from $cidr"

# ---- 3. sshd_config (old one kept as a backup)
$conf = Join-Path $sshDir 'sshd_config'
if (Test-Path $conf) {
    Copy-Item -Force $conf "$conf.teama2a-backup-$(Get-Date -Format yyyyMMdd-HHmmss)"
}
$r = Invoke-Agent @('render-config', '--user', $User)
if ($r.code -ne 0) { throw 'render-config failed' }
Write-Utf8 $conf (($r.out -split "`r?`n") -join "`r`n")

$keys = Join-Path $sshDir 'administrators_authorized_keys'
if (-not (Test-Path $keys)) { Write-Utf8 $keys '' }
& icacls $keys /inheritance:r /grant:r "${SYSTEM}:F" "${ADMINS}:F" | Out-Null
if ($LASTEXITCODE -ne 0) { throw "icacls failed on $keys" }
$r = Invoke-Agent @('sync', '--keys-file', $keys, '--launcher', $launcher)
Write-Host "First key sync: $($r.out)"

& $sshd -t -f $conf
if ($LASTEXITCODE -ne 0) { throw "sshd -t refused $conf (the backup is next to it)" }

# ---- 4. cmd.exe as the SSH shell, firewall from the hub network only
New-Item -Path 'HKLM:\SOFTWARE\OpenSSH' -Force | Out-Null
New-ItemProperty -Path 'HKLM:\SOFTWARE\OpenSSH' -Name DefaultShell `
    -Value (Join-Path $env:WINDIR 'System32\cmd.exe') -PropertyType String -Force | Out-Null
Get-NetFirewallRule -Name 'OpenSSH-Server-In-TCP' -ErrorAction SilentlyContinue | Disable-NetFirewallRule
Get-NetFirewallRule -Name 'TeamA2A-SSH' -ErrorAction SilentlyContinue | Remove-NetFirewallRule
New-NetFirewallRule -Name 'TeamA2A-SSH' -DisplayName 'Team A2A SSH' -Direction Inbound -Protocol TCP `
    -LocalPort 22 -RemoteAddress $cidr -Action Allow | Out-Null

# ---- 5. SYSTEM task that keeps the keys in step with the hub
$taskArgs = (@($pre | ForEach-Object { "`"$_`"" }) + @('--config', "`"$sshJson`"", 'sync',
    '--keys-file', "`"$keys`"", '--launcher', "`"$launcher`"")) -join ' '
$action = New-ScheduledTaskAction -Execute $exe -Argument $taskArgs -WorkingDirectory $dir
$t1 = New-ScheduledTaskTrigger -AtStartup
$t2 = New-ScheduledTaskTrigger -Once -At (Get-Date) -RepetitionInterval (New-TimeSpan -Minutes 15)
$set = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 5) -MultipleInstances IgnoreNew
Register-ScheduledTask -TaskName 'TeamA2A SSH key sync' -Action $action -Trigger @($t1, $t2) `
    -Settings $set -User 'SYSTEM' -RunLevel Highest -Force | Out-Null

Set-Service sshd -StartupType Automatic
Restart-Service sshd
Write-Host "Done. Admins with a key on the hub can now run: ssh_run (Team A2A MCP) to $($rc.name) as $User."
