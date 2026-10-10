@echo off
rem Double-click launcher for the usage widget (no console window stays open).
setlocal
set "WIDGET=%~dp0usage_widget.py"
where pythonw >nul 2>nul
if %errorlevel%==0 (
  start "" pythonw "%WIDGET%" %*
) else (
  start "" python "%~dp0usage_widget.py" %*
)
endlocal
