@echo off
setlocal
cd /d "%~dp0"
echo === Anoma-File build ===

echo [1/3] Checking Python...
py -3 --version
if errorlevel 1 goto :error

echo [2/3] Installing requirements...
py -3 -m pip install -r requirements.txt
if errorlevel 1 goto :error

echo [3/3] Building Anoma-File.exe...
if exist build rmdir /s /q build
if exist dist rmdir /s /q dist
py -3 -m PyInstaller --noconfirm --clean --log-level=INFO Anoma-File.spec
if errorlevel 1 goto :error

echo.
echo ========================================
echo BUILD SUCCESSFUL
echo ========================================
echo EXE: %CD%\dist\Anoma-File.exe
echo.
echo No npm, Node.js or Electron is required.
echo.
pause
exit /b 0
:error
echo.
echo ========================================
echo BUILD FAILED
echo ========================================
pause
exit /b 1
