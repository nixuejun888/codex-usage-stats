@echo off
rem Start the docked Codex usage panel, no console window left behind.
rem Safe to double-click repeatedly: the panel keeps one instance only.
setlocal
set "HERE=%~dp0"
where pythonw >nul 2>nul
if %errorlevel%==0 (
  start "" pythonw "%HERE%hooks\ensure_widget.py"
) else (
  start "" python "%HERE%hooks\ensure_widget.py"
)
endlocal
