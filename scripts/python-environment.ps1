function Resolve-ShockwatchPython {
    param([string]$Python, [Parameter(Mandatory=$true)][string]$ProjectRoot)
    if ($Python) {
        $selectedPython = (Resolve-Path -LiteralPath $Python -ErrorAction Stop).Path
    } else {
        $workspaceRoot = Split-Path -Parent (Split-Path -Parent $ProjectRoot)
        $candidates = @((Join-Path $workspaceRoot '.venv\Scripts\python.exe'), (Join-Path $ProjectRoot '.venv\Scripts\python.exe'))
        $selectedPython = $candidates | Where-Object { Test-Path -LiteralPath $_ -PathType Leaf } | Select-Object -First 1
        if (-not $selectedPython) { throw 'No virtual environment found. Supply -Python with an accessible interpreter path.' }
        $selectedPython = (Resolve-Path -LiteralPath $selectedPython).Path
    }
    if (-not (Test-Path -LiteralPath $selectedPython -PathType Leaf)) { throw "Interpreter is not a file: $selectedPython" }
    $environmentRoot = Split-Path -Parent (Split-Path -Parent $selectedPython)
    [pscustomobject]@{ PythonPath = $selectedPython; EnvironmentRoot = $environmentRoot; IsVenv = (Test-Path -LiteralPath (Join-Path $environmentRoot 'pyvenv.cfg')) }
}

function Test-ShockwatchPython {
    param([Parameter(Mandatory=$true)][string]$PythonPath)
    try {
        & $PythonPath --version
        if ($LASTEXITCODE -ne 0) { throw "Interpreter exited with code $LASTEXITCODE" }
    } catch {
        throw "Python could not start at $PythonPath. Its base interpreter and runtime files must be accessible. $($_.Exception.Message)"
    }
}
