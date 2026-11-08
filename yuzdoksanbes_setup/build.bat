@echo off
REM ============================================================
REM  YUZDOKSANBES POS - build the Windows installer (one click)
REM
REM   1. clean build environment (.build-venv) from requirements.lock
REM   2. PyInstaller  -> dist\YUZDOKSANBES_POS\YUZDOKSANBES_POS.exe
REM   3. Inno Setup   -> output\YUZDOKSANBES_POS_Kurulum_<version>.exe
REM
REM  Needs (build PC only, NOT the cafe PC):
REM    - Python 3.12   (https://www.python.org/downloads/)
REM    - Inno Setup 6  (winget install JRSoftware.InnoSetup --scope user)
REM  Version comes from ..\version.py - bump it there for each release.
REM ============================================================
setlocal
cd /d "%~dp0"

echo ============================================================
echo  YUZDOKSANBES POS - building the installer
echo ============================================================

REM --- 1. clean, pinned build environment -----------------------
set "PY=.build-venv\Scripts\python.exe"
if not exist "%PY%" (
  echo [1/4] Creating clean build environment ...
  py -3.12 -m venv .build-venv 2>nul || python -m venv .build-venv
  if errorlevel 1 ( echo [ERROR] Could not create .build-venv - is Python 3.12 installed? & pause & exit /b 1 )
) else (
  echo [1/4] Using existing .build-venv
)
echo       Installing the frozen versions from requirements.lock ...
"%PY%" -m pip install --disable-pip-version-check -q -r requirements.lock
if errorlevel 1 ( echo [ERROR] Dependency install failed. & pause & exit /b 1 )

REM --- 2. version ---------------------------------------------
for /f %%v in ('"%PY%" make_version_info.py') do set "APPVER=%%v"
if not defined APPVER ( echo [ERROR] Could not read ..\version.py & pause & exit /b 1 )
echo [2/4] Version %APPVER%

REM --- 3. PyInstaller -----------------------------------------
echo [3/4] Building YUZDOKSANBES_POS.exe (PyInstaller) ...
"%PY%" -m PyInstaller --noconfirm --clean --log-level WARN ^
  --distpath dist --workpath build\pyinstaller yuzdoksanbes_pos.spec
if errorlevel 1 ( echo [ERROR] PyInstaller build failed. & pause & exit /b 1 )

REM --- 3b. Self-test the BUILT exe --------------------------------
REM  Generates real PDF reports + a QR code inside the packaged program, with a
REM  clean PATH (Windows only) so nothing from this build PC's Python/Git can
REM  hide a missing library. No installer is made if this fails.
echo       Self-testing the built program ...
setlocal
set "PATH=%SystemRoot%\System32;%SystemRoot%"
"dist\YUZDOKSANBES_POS\YUZDOKSANBES_POS.exe" --selftest
set "ST=%errorlevel%"
endlocal & set "SELFTEST_RC=%ST%"
if not "%SELFTEST_RC%"=="0" ( echo [ERROR] Self-test FAILED - see above. No installer was built. & pause & exit /b 1 )

REM --- 4. Inno Setup ------------------------------------------
set "ISCC=%LOCALAPPDATA%\Programs\Inno Setup 6\ISCC.exe"
if not exist "%ISCC%" set "ISCC=%ProgramFiles(x86)%\Inno Setup 6\ISCC.exe"
if not exist "%ISCC%" set "ISCC=%ProgramFiles%\Inno Setup 6\ISCC.exe"
if not exist "%ISCC%" (
  echo [ERROR] Inno Setup 6 not found. Install it with:
  echo         winget install JRSoftware.InnoSetup --scope user
  pause & exit /b 1
)
echo [4/4] Building the installer (Inno Setup) ...
"%ISCC%" /Q /DAppVersion=%APPVER% installer.iss
if errorlevel 1 ( echo [ERROR] Installer build failed. & pause & exit /b 1 )

echo.
echo [OK] Installer ready:
echo      %~dp0output\YUZDOKSANBES_POS_Kurulum_%APPVER%.exe
echo.
if not "%1"=="/nopause" pause
