param(
    [Parameter(Mandatory=$true)][string]$Runtime,
    [string]$TaskName = 'Shockwatch-Paper-Yahoo',
    [string]$Python
)
$ErrorActionPreference = 'Stop'
$projectRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..')).Path
$runtimeRoot = (Resolve-Path -LiteralPath $Runtime).Path
. (Join-Path $PSScriptRoot 'python-environment.ps1')
if (-not $Python) {
    $workspacePython = Join-Path (Split-Path -Parent $projectRoot) '.venv\Scripts\python.exe'
    if (Test-Path -LiteralPath $workspacePython -PathType Leaf) { $Python = $workspacePython }
}
$environment = Resolve-ShockwatchPython -Python $Python -ProjectRoot $projectRoot
$pythonPath = $environment.PythonPath
Test-ShockwatchPython -PythonPath $pythonPath
$pythonWindowless = Join-Path (Split-Path -Parent $pythonPath) 'pythonw.exe'
if (-not (Test-Path -LiteralPath $pythonWindowless -PathType Leaf)) { throw "Missing $pythonWindowless" }
$identity = Get-Content -LiteralPath (Join-Path $runtimeRoot 'dataset.json') -Raw | ConvertFrom-Json
if ($identity.mode -ne 'paper' -or $identity.source -ne 'yahoo') { throw 'Only a paper-Yahoo runtime may use this task.' }
if (-not (Test-Path -LiteralPath (Join-Path $runtimeRoot 'paper.db') -PathType Leaf)) { throw 'Complete a manual paper run first.' }
if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) { throw 'Task already exists; review without overwriting.' }
$runner = Join-Path $PSScriptRoot 'run-paper-yahoo-task.py'
$arguments = '"' + $runner + '" --runtime "' + $runtimeRoot + '"'
$action = New-ScheduledTaskAction -Execute $pythonWindowless -Argument $arguments -WorkingDirectory $projectRoot
$trigger = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) -RepetitionInterval (New-TimeSpan -Minutes 5)
$settings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -StartWhenAvailable -WakeToRun -ExecutionTimeLimit (New-TimeSpan -Minutes 10) -DontStopIfGoingOnBatteries
$currentUser = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
$principal = New-ScheduledTaskPrincipal -UserId $currentUser -LogonType Interactive -RunLevel Limited
Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Settings $settings -Principal $principal -Description 'Paper Yahoo observation only. Five-minute stock poll, hourly option snapshot during exchange hours; no orders or Git commits.'
Write-Output "Registered $TaskName. Private logs: $runtimeRoot\logs\scheduled-task.jsonl. STOP sentinel pauses after source failure."
Write-Output 'The task uses an interactive user logon. Physical sleep/wake, reboot and logged-out operation are not thereby verified.'
