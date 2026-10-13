param([string]$Output = 'work\synthetic-local', [string]$Python)
$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
. (Join-Path $PSScriptRoot 'python-environment.ps1')
$environment = Resolve-ShockwatchPython -Python $Python -ProjectRoot $projectRoot
$pythonPath = $environment.PythonPath
Test-ShockwatchPython -PythonPath $pythonPath
Set-Location -LiteralPath $projectRoot
$outputRoot = [System.IO.Path]::GetFullPath((Join-Path $projectRoot $Output))
$verificationFile = Join-Path $outputRoot 'VERIFICATION.json'
if (-not (Test-Path -LiteralPath $verificationFile)) {
    & $pythonPath -m shockwatch synthetic-demo --out $outputRoot
    if ($LASTEXITCODE -ne 0) { throw 'Synthetic demonstration failed; inspect output and use a new output directory after fixes.' }
}
$verification = Get-Content -LiteralPath $verificationFile -Raw | ConvertFrom-Json
if (-not ($verification.core_replay_equal -and $verification.forecast_numerical_replay_equal -and $verification.future_forecast_ordering -and $verification.duplicate_idempotent -and $verification.scenario_evaluation.all_measured_checks_passed -and $verification.experiment_execution_complete -and $verification.restore.passed -and $verification.forward_recovery.passed)) { throw 'Required synthetic checks did not pass; local preview was not started.' }
& $pythonPath -m shockwatch serve --site (Join-Path $outputRoot 'site') --port 8765
if ($LASTEXITCODE -ne 0) { throw 'Preview server failed.' }
