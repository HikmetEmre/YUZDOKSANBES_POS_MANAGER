@echo off
REM ============================================================
REM  YUZDOKSANBES POS - Launcher
REM  Starts the server. Tablets/phones on the same WiFi open
REM  http://<this-PC-IP>:8000 in a browser.
REM ============================================================
setlocal
cd /d "%~dp0"
title YUZDOKSANBES POS

REM --- Prefer the venv made by install.bat; fall back to system Python ---
set "PYEXE=.venv\Scripts\python.exe"
if not exist "%PYEXE%" (
  echo [INFO] .venv not found - using system Python.
  echo        If launch fails, run install.bat first.
  set "PYEXE=python"
)

echo ============================================================
echo  Starting YUZDOKSANBES POS ...
echo  This window must stay OPEN while the cafe is using the POS.
echo  Press Ctrl+C (or just close this window) to stop the server.
echo ============================================================
echo.

REM --- Show this PC's LAN IPv4 addresses so staff know the URL ---
REM  Matching "IPv4" works on English and Turkish Windows alike; the
REM  helper trims the leading space cmd leaves after the colon.
echo Connect tablets/phones to one of these addresses on port 8000:
for /f "tokens=2 delims=:" %%a in ('ipconfig ^| findstr /c:"IPv4"') do call :showip %%a
echo.

"%PYEXE%" order_server.py

echo.
echo Server stopped.
pause
exit /b 0

:showip
echo    http://%~1:8000
exit /b 0
