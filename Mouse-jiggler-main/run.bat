@echo off
REM Double-click to start the mouse jiggler. Press Ctrl+C or close this window to stop.
cd /d "%~dp0"
where python >nul 2>nul
if %errorlevel%==0 (
    python jiggler.py %*
) else (
    py jiggler.py %*
)
pause
