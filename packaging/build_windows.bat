@echo off
REM Windows packaging launcher (double-click to run).
REM NOTE: keep this file ASCII-only - cmd.exe reads .bat files with the
REM       console OEM code page, so non-ASCII text may render as garbage.
setlocal
cd /d "%~dp0.."
powershell -ExecutionPolicy Bypass -File "packaging\build_windows.ps1" %*
if errorlevel 1 (
  echo.
  echo Build failed. See the log above.
  pause
  exit /b 1
)
echo.
echo Build finished. Artifacts are in packaging\dist\
pause
