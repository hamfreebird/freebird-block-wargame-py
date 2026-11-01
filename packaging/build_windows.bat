@echo off
REM Windows 打包快捷入口（双击即可）
setlocal
chcp 65001 >nul
cd /d "%~dp0.."
powershell -ExecutionPolicy Bypass -File "packaging\build_windows.ps1" %*
if errorlevel 1 (
  echo.
  echo 打包失败，请查看上方日志。
  pause
  exit /b 1
)
echo.
echo 打包完成，产物位于 packaging\dist\
pause
