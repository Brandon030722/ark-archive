@echo off
setlocal
pushd "%~dp0" || exit /b 1
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "scripts\bootstrap_windows.ps1" -Stop %*
set "ark_exit=%ERRORLEVEL%"
popd
if not "%ark_exit%"=="0" if not defined CI pause
exit /b %ark_exit%
