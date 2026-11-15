# Remove the Team A2A receiver task for the current user.
$taskName = 'TeamA2A Receiver'
Stop-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
Unregister-ScheduledTask -TaskName $taskName -Confirm:$false -ErrorAction SilentlyContinue
Get-CimInstance Win32_Process -Filter "Name='pythonw.exe'" |
    Where-Object { $_.CommandLine -like '*receiver.py*' } |
    ForEach-Object { Stop-Process -Id $_.ProcessId -Force }
Write-Host "Removed '$taskName'."
