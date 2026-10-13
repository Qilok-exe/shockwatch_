@echo off
setlocal
cd /d "%~dp0.."
".venv\Scripts\python.exe" -m shockwatch run-once --inbox runtime\inbox --db runtime\monitor.db --model runtime\model.json --policy runtime\policy.json --calendar runtime\sessions.csv --config config\runtime.json
exit /b %errorlevel%
