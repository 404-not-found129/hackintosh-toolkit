@echo off
setlocal
rem Native launcher for install.py (see its own docstring) on Windows -
rem double-click this, or run it from Command Prompt/PowerShell. Only job
rem here is finding a Python interpreter and running install.py from this
rem script's own directory regardless of where it was invoked from.
rem install.py handles everything else itself, including relaunching
rem elevated with a UAC prompt when it needs to (see its _self_elevate())
rem - don't run this from an elevated prompt yourself, just run it plain.

cd /d "%~dp0"

where python >nul 2>nul
if %ERRORLEVEL% EQU 0 (
    python install.py %*
    goto :end
)

where py >nul 2>nul
if %ERRORLEVEL% EQU 0 (
    py install.py %*
    goto :end
)

echo Python not found. Install it first from:
echo     https://www.python.org/downloads/windows/
echo Make sure to check "Add python.exe to PATH" during setup, then run
echo this again.
pause
exit /b 1

:end
if %ERRORLEVEL% NEQ 0 pause
