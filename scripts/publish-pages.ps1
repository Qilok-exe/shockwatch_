param(
    [Parameter(Mandatory=$true)][string]$Runtime,
    [string]$Experiment,
    [string]$Scenarios,
    [string]$Python
)
$ErrorActionPreference = 'Stop'
$projectRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..')).Path
$workspaceRoot = Split-Path -Parent $projectRoot
if (-not $Python) {
    $candidates = @((Join-Path $workspaceRoot '.venv\Scripts\python.exe'), (Join-Path $projectRoot '.venv\Scripts\python.exe'))
    $Python = $candidates | Where-Object { Test-Path -LiteralPath $_ -PathType Leaf } | Select-Object -First 1
    if (-not $Python) { throw 'No Python virtual environment found; supply -Python.' }
}
$Python = (Resolve-Path -LiteralPath $Python -ErrorAction Stop).Path
$runtimeRoot = (Resolve-Path -LiteralPath $Runtime -ErrorAction Stop).Path
$database = Join-Path $runtimeRoot 'monitor.db'
if (-not (Test-Path -LiteralPath $database -PathType Leaf)) { throw 'Runtime has no committed monitor.db' }
$docs = Join-Path $projectRoot 'docs'
$arguments = @('-m','shockwatch.publication','export','--db',$database,'--docs',$docs)
if ($Experiment) { $arguments += @('--experiment',(Resolve-Path -LiteralPath $Experiment -ErrorAction Stop).Path) }
if ($Scenarios) { $arguments += @('--scenarios',(Resolve-Path -LiteralPath $Scenarios -ErrorAction Stop).Path) }
Push-Location $projectRoot
try {
    & $Python @arguments
    if ($LASTEXITCODE -ne 0) { throw 'Public export failed' }
    & $Python -m shockwatch.publication validate --docs $docs
    if ($LASTEXITCODE -ne 0) { throw 'Public link/file validation failed' }
} finally { Pop-Location }
Write-Output 'Sanitized GitHub Pages output is ready in docs. Review and commit/push manually; this script never commits.'
