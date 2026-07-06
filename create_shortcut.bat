@echo off
REM ============================================================
REM  YUZDOKSANBES POS - create a Desktop shortcut (logo icon)
REM  Makes logo.ico from logo.png (if needed), then drops a
REM  "YUZDOKSANBES POS" shortcut on the Desktop that runs start.bat.
REM  Run this on each PC after install.bat.
REM ============================================================
setlocal
cd /d "%~dp0"

REM --- Pick Python (venv first) to generate the .ico from logo.png ---
set "PYEXE=.venv\Scripts\python.exe"
if not exist "%PYEXE%" set "PYEXE=python"

if exist "logo.ico" goto :makelink
if not exist "logo.png" (
  echo [WARN] logo.png not found - shortcut will use the default icon.
  goto :makelink
)

echo Generating logo.ico from logo.png ...
"%PYEXE%" -c "from PIL import Image;i=Image.open('logo.png').convert('RGBA');s=max(i.size);c=Image.new('RGBA',(s,s),(0,0,0,0));c.paste(i,((s-i.size[0])//2,(s-i.size[1])//2),i);c.save('logo.ico',sizes=[(16,16),(32,32),(48,48),(64,64),(128,128),(256,256)])"
if errorlevel 1 echo [WARN] Could not build logo.ico (Pillow missing?). Using default icon.

:makelink
set "ICON=%~dp0logo.ico"
if not exist "%ICON%" set "ICON=%~dp0start.bat"

echo Creating the Desktop shortcut ...
powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "$w=New-Object -ComObject WScript.Shell;" ^
  "$l=Join-Path ([Environment]::GetFolderPath('Desktop')) 'YUZDOKSANBES POS.lnk';" ^
  "$s=$w.CreateShortcut($l);" ^
  "$s.TargetPath='%~dp0start.bat';" ^
  "$s.WorkingDirectory='%~dp0';" ^
  "$s.IconLocation='%ICON%,0';" ^
  "$s.Description='YUZDOKSANBES POS - cafe order system';" ^
  "$s.WindowStyle=1;" ^
  "$s.Save();" ^
  "Write-Host ('Created: ' + $l)"

echo.
echo [OK] Done. Look for "YUZDOKSANBES POS" on your Desktop.
echo.
pause
