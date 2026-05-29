@echo off
REM ============================================================
REM Rebuild Python sidecar + launch test app in dev mode
REM ============================================================
REM Use this to test Wave-1 changes (or any backend code change)
REM in the desktop app. First run takes 5-8 min for PyInstaller;
REM subsequent runs are faster.
REM
REM Closes the app window with Ctrl+C in this terminal.
REM ============================================================

setlocal
set ROOT=%~dp0
cd /d "%ROOT%"

echo.
echo [1/3] Rebuilding Python sidecar (backend.exe)...
echo       This compiles your Python code changes into the bundled binary.
echo       Takes 5-8 minutes the first time, ~2-3 minutes after that.
echo.

REM Make sure PyInstaller is installed in the venv. It's a build-time tool,
REM not in requirements.txt, so a fresh venv won't have it. This is a
REM no-op if already installed.
backend\venv\Scripts\pip install pyinstaller --quiet

backend\venv\Scripts\python.exe -m PyInstaller backend.spec --clean
if errorlevel 1 (
    echo.
    echo *** PyInstaller failed. Check backend\venv exists, see error above. ***
    echo *** If venv missing: python -m venv backend\venv ; backend\venv\Scripts\pip install -r backend\requirements.txt ***
    pause
    exit /b 1
)

echo.
echo [2/3] Copying new sidecar to Tauri binaries folder...
copy /Y "dist\backend.exe" "desktop_app\src-tauri\binaries\backend-x86_64-pc-windows-msvc.exe"
if errorlevel 1 (
    echo *** Copy failed. ***
    pause
    exit /b 1
)

echo.
echo [3/3] Launching Tauri app in dev mode...
echo       The app window should open in 10-30 seconds.
echo       Backend logs print here in this terminal.
echo       Press Ctrl+C in this terminal to quit.
echo.

cd desktop_app
call npm run tauri dev
