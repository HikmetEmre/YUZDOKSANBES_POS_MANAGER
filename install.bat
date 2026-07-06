@echo off
REM ============================================================
REM  YUZDOKSANBES POS - Dependency Installer
REM  Creates a local virtual environment (.venv) and installs
REM  everything from requirements.txt. Run this ONCE on a new PC.
REM ============================================================
setlocal
cd /d "%~dp0"

echo ============================================================
echo  YUZDOKSANBES POS - installing dependencies
echo ============================================================
echo.

REM --- Find a Python launcher (prefer the 'py' launcher) ---
set "PY="
where py >nul 2>nul && set "PY=py"
if not defined PY ( where python >nul 2>nul && set "PY=python" )
if not defined PY (
  echo [ERROR] Python was not found on this PC.
  echo         Install Python 3.10 or newer from https://www.python.org/downloads/
  echo         and tick "Add Python to PATH" during setup, then run this again.
  echo.
  pause
  exit /b 1
)
echo Using Python launcher: %PY%
echo.

echo [1/3] Creating virtual environment (.venv) ...
%PY% -m venv .venv
if errorlevel 1 ( echo [ERROR] Could not create the virtual environment. & pause & exit /b 1 )

echo [2/3] Upgrading pip ...
".venv\Scripts\python.exe" -m pip install --upgrade pip

echo [3/3] Installing dependencies from requirements.txt ...
".venv\Scripts\python.exe" -m pip install -r requirements.txt
if errorlevel 1 ( echo [ERROR] Dependency installation failed. & pause & exit /b 1 )

echo.
echo [OK] All set. Double-click start.bat to launch the POS.
echo.
pause
