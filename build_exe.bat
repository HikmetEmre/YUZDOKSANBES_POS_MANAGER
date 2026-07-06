@echo off
REM ============================================================
REM  YUZDOKSANBES POS - build a standalone Windows .exe
REM  Produces dist\yuzdoksanbes_pos.exe (no Python needed to run).
REM  Run install.bat first so the .venv with deps exists.
REM ============================================================
setlocal
cd /d "%~dp0"

echo ============================================================
echo  Building YUZDOKSANBES POS .exe (PyInstaller)
echo ============================================================
echo.

set "PYEXE=.venv\Scripts\python.exe"
if not exist "%PYEXE%" (
  echo [INFO] .venv not found - using system Python.
  set "PYEXE=python"
)

echo [1/2] Installing PyInstaller ...
"%PYEXE%" -m pip install pyinstaller
if errorlevel 1 ( echo [ERROR] Could not install PyInstaller. & pause & exit /b 1 )

echo [2/2] Building (this can take a minute) ...
REM  static\ is bundled (read-only, served from inside the exe).
REM  pos.db, backups\ and secret_key.txt are created next to the exe at runtime.
"%PYEXE%" -m PyInstaller --noconfirm --onefile --console ^
  --name yuzdoksanbes_pos ^
  --add-data "static;static" ^
  --collect-submodules uvicorn ^
  order_server.py
if errorlevel 1 ( echo [ERROR] Build failed. & pause & exit /b 1 )

echo.
echo [OK] Built: dist\yuzdoksanbes_pos.exe
echo.
echo Before first run, copy these NEXT TO the .exe (same folder):
echo    - logo.png                     (optional - report header logo)
echo    - font.ttf or DejaVuSans.ttf   (optional - Turkish/currency text in PDFs)
echo Writable data (pos.db, backups\, secret_key.txt) is created there automatically.
echo.
echo Tip: set POS_CHECKOUT_PIN / POS_WAITER_PIN / POS_SECRET_KEY as environment
echo      variables so secrets are not baked into the build.
echo.
pause
