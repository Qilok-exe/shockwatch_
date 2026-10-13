param(
    [Parameter(Mandatory=$true)][string]$Runtime,
    [string]$TaskName = 'Shockwatch-Synthetic',
    [string]$Python
)
$ErrorActionPreference = 'Stop'
$projectRoot = (Resolve-Path -LiteralPath (Split-Path -Parent $PSScriptRoot)).Path
$runtimeRoot = (Resolve-Path -LiteralPath $Runtime).Path
. (Join-Path $PSScriptRoot 'python-environment.ps1')
$environment = Resolve-ShockwatchPython -Python $Python -ProjectRoot $projectRoot
$pythonPath = $environment.PythonPath
Test-ShockwatchPython -PythonPath $pythonPath
$pythonWindowless = Join-Path (Split-Path -Parent $pythonPath) 'pythonw.exe'
if (-not (Test-Path -LiteralPath $pythonWindowless -PathType Leaf)) { throw "Missing windowless interpreter: $pythonWindowless" }
$runner = Join-Path $PSScriptRoot 'run-synthetic-task.py'
if (-not (Test-Path -LiteralPath $runner -PathType Leaf)) { throw "Missing scheduled task runner: $runner" }
foreach ($relative in @('deployment.json','dataset.json','model.json','policy.json','sessions.csv','config.json','monitor.db','cycle.json')) {
    if (-not (Test-Path -LiteralPath (Join-Path $runtimeRoot $relative))) { throw "Missing $relative. Complete a manual synthetic cycle first." }
}
$dataset = Get-Content -LiteralPath (Join-Path $runtimeRoot 'dataset.json') -Raw | ConvertFrom-Json
$config = Get-Content -LiteralPath (Join-Path $runtimeRoot 'config.json') -Raw | ConvertFrom-Json
if ($dataset.mode -ne 'synthetic' -or $config.mode -ne 'synthetic' -or $config.notifications.enabled) { throw 'Only a notifications-disabled synthetic runtime may use this task.' }
& $pythonPath -m shockwatch doctor --runtime $runtimeRoot
if ($LASTEXITCODE -ne 0) { throw 'Doctor failed; task was not registered.' }
if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) { throw 'Task already exists; review it without overwriting.' }
$arguments = '"' + $runner + '" --python "' + $pythonPath + '" --runtime "' + $runtimeRoot + '" --task-name "' + $TaskName + '"'
$action = New-ScheduledTaskAction -Execute $pythonWindowless -Argument $arguments -WorkingDirectory $projectRoot
$trigger = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) -RepetitionInterval (New-TimeSpan -Minutes 5)
$settings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -StartWhenAvailable -WakeToRun -ExecutionTimeLimit (New-TimeSpan -Minutes 10) -DontStopIfGoingOnBatteries
$currentUser = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
$principal = New-ScheduledTaskPrincipal -UserId $currentUser -LogonType Interactive -RunLevel Limited
Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Settings $settings -Principal $principal -Description 'Finite synthetic virtual clock, shared worker, outcome ledger and read-only website every five minutes.'
Write-Output "Registered $TaskName. Each wake advances one virtual cohort; these are artificial observations."
Write-Output "Logs: $runtimeRoot\logs\scheduled-task.jsonl and scheduled-task-latest.json. The task disables itself after its finite source is exhausted."
Write-Output 'The task starts on AC power; a wake already running may finish after a switch to battery.'
Write-Output ('Stop: & "' + $pythonPath + '" -m shockwatch stop --runtime "' + $runtimeRoot + '" ; then Disable-ScheduledTask -TaskName "' + $TaskName + '".')
Write-Output 'Logged-out, power wake, suspend/resume, reboot/login and overlap behavior still require actual tests.'
