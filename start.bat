@echo off
rem Windows launcher. Keep this file ASCII-only: cmd.exe parses batch files with
rem the OEM code page and non-ASCII bytes can break line parsing.
rem All real work (platform detection, openpyxl check, args) is in start.py.
chcp 65001 >nul
setlocal
cd /d "%~dp0"

set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8

where python >nul 2>nul
if errorlevel 1 (
    echo.
    echo [ERROR] python command not found.
    echo.
    echo Install Python 3.10 or newer and tick "Add python.exe to PATH":
    echo   https://www.python.org/downloads/windows/
    echo.
    pause
    exit /b 1
)

python start.py %*
set EXITCODE=%ERRORLEVEL%

echo.
if not "%EXITCODE%"=="0" echo [service exited with code %EXITCODE%]
pause
exit /b %EXITCODE%
