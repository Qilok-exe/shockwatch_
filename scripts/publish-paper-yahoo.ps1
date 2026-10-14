param(
    [Parameter(Mandatory=$true)][string]$Runtime,
    [string]$Python
)
$ErrorActionPreference = 'Stop'
$projectRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..')).Path
$workspaceRoot = Split-Path -Parent $projectRoot
if (-not $Python) {
    $Python = @((Join-Path $workspaceRoot '.venv\Scripts\python.exe'), (Join-Path $projectRoot '.venv\Scripts\python.exe')) |
        Where-Object { Test-Path -LiteralPath $_ -PathType Leaf } | Select-Object -First 1
    if (-not $Python) { throw 'No Python virtual environment found; supply -Python.' }
}
$runtimeRoot = (Resolve-Path -LiteralPath $Runtime).Path
$database = Join-Path $runtimeRoot 'paper.db'
$identity = Get-Content -LiteralPath (Join-Path $runtimeRoot 'dataset.json') -Raw | ConvertFrom-Json
if ($identity.mode -ne 'paper' -or $identity.source -ne 'yahoo') { throw 'Only a paper-Yahoo runtime can be published.' }
if (-not (Test-Path -LiteralPath $database -PathType Leaf)) { throw 'Missing paper database.' }
$docs = Join-Path $projectRoot 'docs'
$archive = Join-Path $docs 'archive\synthetic_20261014'
$syntheticSite = if (Test-Path -LiteralPath $archive) { $archive } else { $docs }
Push-Location $projectRoot
try {
    & $Python -m shockwatch.paper_publication export --db $database --docs $docs --synthetic-site $syntheticSite
    if ($LASTEXITCODE -ne 0) { throw 'Paper public export failed.' }
    & $Python -m shockwatch.paper_publication validate --docs $docs
    if ($LASTEXITCODE -ne 0) { throw 'Paper public validation failed.' }
} finally { Pop-Location }
Write-Output 'Bounded derived paper-Yahoo site is ready in docs. This command never commits or pushes.'
