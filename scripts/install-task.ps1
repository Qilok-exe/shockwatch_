param([string]$TaskName = "Shockwatch-Pilot")
$ErrorActionPreference = "Stop"
$ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$PythonPath = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
foreach ($Relative in @(".venv\Scripts\python.exe", "runtime\model.json", "runtime\policy.json", "runtime\sessions.csv", "config\runtime.json")) {
    if (-not (Test-Path (Join-Path $ProjectRoot $Relative))) {
        throw "Missing $Relative. Complete the real-data shadow setup first."
    }
}
if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
    throw "Task already exists. Review the existing task before changing it."
}
$Arguments = '-m shockwatch run-once --inbox "runtime\inbox" --db "runtime\monitor.db" --model "runtime\model.json" --policy "runtime\policy.json" --calendar "runtime\sessions.csv" --config "config\runtime.json"'
$Action = New-ScheduledTaskAction -Execute $PythonPath -Argument $Arguments -WorkingDirectory $ProjectRoot
$Trigger = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) -RepetitionInterval (New-TimeSpan -Minutes 5)
$Settings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -StartWhenAvailable -WakeToRun -ExecutionTimeLimit (New-TimeSpan -Minutes 10) -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
$CurrentUser = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
$Principal = New-ScheduledTaskPrincipal -UserId $CurrentUser -LogonType Interactive -RunLevel Limited
Register-ScheduledTask -TaskName $TaskName -Action $Action -Trigger $Trigger -Settings $Settings -Principal $Principal -Description "SHOCKWATCH pilot: check inbox and health every five minutes."
Write-Output "Registered $TaskName. Runs while this Windows user is logged in, including a locked screen."
Write-Output "Wake depends on supported power settings. Test suspend and restart on this laptop."
