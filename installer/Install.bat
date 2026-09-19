@echo off
title Testbook Error Logger - Install
cd /d "%~dp0"

echo.
echo   Testbook Error Logger - one-click install
echo   -----------------------------------------
echo   This sets up Ollama, the AI model, the server and the Chrome extension.
echo   Nothing is installed system-wide and no admin rights are needed.
echo.

rem Files extracted from a downloaded zip carry a "mark of the web" that makes
rem PowerShell refuse to run them. Clear it before doing anything else.
powershell -NoProfile -ExecutionPolicy Bypass -Command "Get-ChildItem -Path '%~dp0' -Recurse -File | Unblock-File -ErrorAction SilentlyContinue" >nul 2>&1

powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0install.ps1" %*
set "RC=%ERRORLEVEL%"

if not "%RC%"=="0" (
  echo.
  echo   Install did not finish cleanly ^(exit code %RC%^).
  echo   Read the messages above - they say which step failed.
  echo.
  pause
)
exit /b %RC%
