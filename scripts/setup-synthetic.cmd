@echo off
setlocal
if not "%~1"=="" (
    set "SW_VALIDATE_PYTHON=%~f1"
) else if exist "%~dp0..\..\..\.venv\Scripts\python.exe" (
    set "SW_VALIDATE_PYTHON=%~dp0..\..\..\.venv\Scripts\python.exe"
) else (
    set "SW_VALIDATE_PYTHON=%~dp0..\.venv\Scripts\python.exe"
)
if not exist "%SW_VALIDATE_PYTHON%" (
    echo Python interpreter not found: "%SW_VALIDATE_PYTHON%"
    exit /b 1
)
"%SW_VALIDATE_PYTHON%" --version
if errorlevel 1 exit /b 1
uv --cache-dir "%~dp0..\..\..\.uv-cache" pip install --python "%SW_VALIDATE_PYTHON%" -r "%~dp0..\requirements-verified.txt"
if errorlevel 1 exit /b 1
uv --cache-dir "%~dp0..\..\..\.uv-cache" pip install --python "%SW_VALIDATE_PYTHON%" -e "%~dp0.." --no-deps
if errorlevel 1 exit /b 1
cd /d "%~dp0.."
"%SW_VALIDATE_PYTHON%" "scripts\verify-implementation.py"
exit /b %errorlevel%
