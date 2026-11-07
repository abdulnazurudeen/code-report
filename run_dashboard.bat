@echo off
REM Starts the Code Review Audit dashboard and opens it in your browser.
cd /d "%~dp0"
where py >NUL 2>NUL
if %errorlevel%==0 (
  py -3 audit.py serve --open
) else (
  python audit.py serve --open
)
if errorlevel 1 (
  echo.
  echo Could not start the dashboard. Is Python 3.9+ installed? Get it from https://www.python.org/downloads/
)
pause
