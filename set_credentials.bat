@echo off
REM ============================================================
REM  YUZDOKSANBES POS - set login PINs (saved as user env vars)
REM  Run this ONCE per PC before going live. The PINs are stored
REM  for your Windows user via setx and are NOT written into any
REM  source file, so they never end up in git or a backup.
REM ============================================================
setlocal
echo ============================================================
echo  Set the POS login PINs
echo ============================================================
echo Use 4-16 English letters and/or digits (case-sensitive), e.g. kasa2026.
echo No Turkish letters or spaces. Give the Checkout password to the desk only.
echo.

set /p CPIN="Checkout (admin) PIN: "
set /p WPIN="Waiter PIN: "

if "%CPIN%"=="" ( echo [ERROR] Checkout PIN cannot be empty. & pause & exit /b 1 )
if "%WPIN%"=="" ( echo [ERROR] Waiter PIN cannot be empty.   & pause & exit /b 1 )
if "%CPIN%"=="%WPIN%" ( echo [ERROR] The two PINs must be different. & pause & exit /b 1 )

setx POS_CHECKOUT_PIN "%CPIN%" >nul
setx POS_WAITER_PIN   "%WPIN%" >nul

echo.
echo [OK] PINs saved for this Windows user.
echo      The signing secret is generated automatically (secret_key.txt).
echo      Close any open POS window and reopen start.bat for the new PINs to apply.
echo.
pause
