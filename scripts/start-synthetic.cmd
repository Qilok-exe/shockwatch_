@echo off
setlocal
if exist "%~dp0..\..\..\.venv\Scripts\python.exe" (
    set "SW_START_PYTHON=%~dp0..\..\..\.venv\Scripts\python.exe"
) else (
    set "SW_START_PYTHON=%~dp0..\.venv\Scripts\python.exe"
)
if not "%~1"=="" (
    set "SW_START_OUTPUT=%~f1"
) else (
    set "SW_START_OUTPUT=%~dp0..\work\synthetic-local"
)
"%SW_START_PYTHON%" --version
if errorlevel 1 exit /b 1
set "OPENBLAS_NUM_THREADS=1"
set "OMP_NUM_THREADS=1"
set "MKL_NUM_THREADS=1"
set "BLIS_NUM_THREADS=1"
set "NUMEXPR_NUM_THREADS=1"
cd /d "%~dp0.."
if not exist "%SW_START_OUTPUT%\VERIFICATION.json" (
    "%SW_START_PYTHON%" -m shockwatch synthetic-demo --out "%SW_START_OUTPUT%"
    if errorlevel 1 exit /b 1
)
"%SW_START_PYTHON%" -c "import json,sys; v=json.load(open(sys.argv[1],encoding='utf-8')); checks=[v.get('core_replay_equal'),v.get('forecast_numerical_replay_equal'),v.get('future_forecast_ordering'),v.get('duplicate_idempotent'),v.get('scenario_evaluation',{}).get('all_measured_checks_passed'),v.get('experiment_execution_complete'),v.get('restore',{}).get('passed'),v.get('forward_recovery',{}).get('passed')]; sys.exit(0 if all(checks) else 1)" "%SW_START_OUTPUT%\VERIFICATION.json"
if errorlevel 1 (
    echo Required verification gates have not passed. Preview was not started.
    exit /b 1
)
"%SW_START_PYTHON%" -m shockwatch serve --site "%SW_START_OUTPUT%\site" --port 8765
exit /b %errorlevel%
