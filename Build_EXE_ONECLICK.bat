@echo off
setlocal enabledelayedexpansion
cd /d "%~dp0"

REM ========= SETTINGS =========
set "APP_PY=mlo_cleaner_gui_dark_multi.py"
set "APP_NAME=MLO_Cleaner"
set "ICON_PNG=icon.png"
REM ============================

echo ==========================================
echo  ONE-CLICK BUILD: %APP_NAME%
echo  Folder: %CD%
echo ==========================================

REM --- basic file checks
if not exist "%APP_PY%" (
  echo [ERROR] Missing %APP_PY% in %CD%
  pause
  exit /b 1
)
if not exist "%ICON_PNG%" (
  echo [ERROR] Missing %ICON_PNG% in %CD%
  pause
  exit /b 1
)

REM --- Disable Store alias (best-effort)
REM (User still can disable manually in Settings > App execution aliases)
REG ADD "HKCU\Software\Microsoft\Windows\CurrentVersion\App Paths\python.exe" /ve /d "" /f >nul 2>nul

REM --- Ensure winget
where winget >nul 2>nul
if errorlevel 1 (
  echo [ERROR] winget not found. Install Python manually from python.org.
  pause
  exit /b 1
)

REM --- Install Python (silently) if no REAL python found
echo [INFO] Installing Python (if needed)...
winget install -e --id Python.Python.3.12 --silent --accept-package-agreements --accept-source-agreements >nul 2>nul

REM --- Find a REAL python (ignore WindowsApps)
set "PYEXE="
for /f "delims=" %%P in ('where python 2^>nul') do (
  echo %%P | find /I "WindowsApps" >nul
  if errorlevel 1 (
    if exist "%%P" (
      set "PYEXE=%%P"
      goto PY_FOUND
    )
  )
)
:PY_FOUND

if "%PYEXE%"=="" (
  echo [ERROR] Python not usable yet.
  echo Fix: Settings ^> Apps ^> App execution aliases ^> turn OFF python.exe/python3.exe
  echo Then run this .bat again.
  pause
  exit /b 1
)

echo [OK] Using Python: %PYEXE%
"%PYEXE%" --version

REM --- venv
echo [INFO] Creating venv...
"%PYEXE%" -m venv .venv
if errorlevel 1 (
  echo [ERROR] venv failed.
  pause
  exit /b 1
)

call .venv\Scripts\activate.bat
if errorlevel 1 (
  echo [ERROR] activate failed.
  pause
  exit /b 1
)

REM --- deps
echo [INFO] Installing build deps...
python -m pip install --upgrade pip >nul
python -m pip install pillow pyinstaller >nul
if errorlevel 1 (
  echo [ERROR] pip install failed.
  pause
  exit /b 1
)

REM --- icon conversion
echo [INFO] Converting icon.png to icon.ico...
python -c "from PIL import Image; im=Image.open('%ICON_PNG%').convert('RGBA'); sizes=[(16,16),(24,24),(32,32),(48,48),(64,64),(128,128),(256,256)]; im.save('icon.ico', sizes=sizes)"
if errorlevel 1 (
  echo [ERROR] icon conversion failed.
  pause
  exit /b 1
)

REM --- build exe
echo [INFO] Building EXE...
pyinstaller --noconfirm --clean --onefile --windowed ^
  --name "%APP_NAME%" ^
  --icon "icon.ico" ^
  --add-data "%ICON_PNG%;." ^
  "%APP_PY%"
if errorlevel 1 (
  echo [ERROR] PyInstaller build failed.
  pause
  exit /b 1
)

echo.
echo ===============================
echo DONE!
echo EXE created: %CD%\dist\%APP_NAME%.exe
echo ===============================
pause
exit /b 0
