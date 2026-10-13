param([string]$Python)
$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
. (Join-Path $PSScriptRoot 'python-environment.ps1')
# Resolve an explicit relative path while still in the caller's directory.
$environment = Resolve-ShockwatchPython -Python $Python -ProjectRoot $projectRoot
Test-ShockwatchPython -PythonPath $environment.PythonPath
Set-Location -LiteralPath $projectRoot
$cachePath = Join-Path (Split-Path -Parent (Split-Path -Parent $projectRoot)) '.uv-cache'
if ($environment.IsVenv) {
    $venvPython = $environment.PythonPath
} else {
    $environmentRoot = Join-Path $projectRoot '.venv'
    if (Test-Path -LiteralPath $environmentRoot) { throw 'Project .venv already exists; supply its interpreter to reuse it without overwriting.' }
    & uv --cache-dir $cachePath venv --python $environment.PythonPath $environmentRoot
    if ($LASTEXITCODE -ne 0) { throw 'uv environment creation failed' }
    $venvPython = Join-Path $environmentRoot 'Scripts\python.exe'
    Test-ShockwatchPython -PythonPath $venvPython
}
Write-Output "Using isolated environment: $venvPython"
& uv --cache-dir $cachePath pip install --python $venvPython -r 'requirements-verified.txt'
if ($LASTEXITCODE -ne 0) { throw 'Dependency installation failed' }
& uv --cache-dir $cachePath pip install --python $venvPython -e . --no-deps
if ($LASTEXITCODE -ne 0) { throw 'Package installation failed' }
& $venvPython 'scripts\verify-implementation.py'
if ($LASTEXITCODE -ne 0) { throw 'Verification failed; acceptance gates remain incomplete' }
