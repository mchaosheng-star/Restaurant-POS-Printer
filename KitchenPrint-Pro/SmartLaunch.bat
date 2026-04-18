@echo off
title Sakura KitchenPrint-Pro
cd /d "%~dp0"

set PRINT_CAPTURE_MDNS_HOST=192.168.1.251
set PRINT_CAPTURE_AIRPRINT_NAME=SakuraKitchenPrintPro

IF NOT EXIST "app.py" (
  echo ERROR: app.py not found in:
  echo %cd%
  pause
  exit /b 1
)

start "Sakura KitchenPrint-Pro Server" powershell -NoProfile -ExecutionPolicy Bypass -Command "Set-Location -LiteralPath '%~dp0'; $env:PRINT_CAPTURE_MDNS_HOST='192.168.1.251'; $env:PRINT_CAPTURE_AIRPRINT_NAME='SakuraKitchenPrintPro'; python .\app.py"
timeout /t 3 /nobreak > nul
exit /b 0
