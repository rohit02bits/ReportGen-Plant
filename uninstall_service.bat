@echo off
:: Stops and removes the Plant Batching Report Generator Windows Service.
:: Must be run as Administrator.

set SERVICE_NAME=PlantReportGenerator
cd /d "%~dp0"

net session >nul 2>&1
if errorlevel 1 (
    echo This script must be run as Administrator.
    pause
    exit /b 1
)

echo Stopping service...
sc stop %SERVICE_NAME% >nul 2>&1
timeout /t 5 /nobreak >nul

echo Removing service...
sc delete %SERVICE_NAME%
pause
