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

:: Wait up to 30s for the service to stop (ping is used as the delay because
:: timeout fails when input is redirected)
set /a WAITED=0
:wait_stop
sc query %SERVICE_NAME% >nul 2>&1
if errorlevel 1 goto remove
sc query %SERVICE_NAME% | find "STOPPED" >nul 2>&1
if not errorlevel 1 goto remove
if %WAITED% geq 30 goto remove
ping -n 2 127.0.0.1 >nul
set /a WAITED+=1
goto wait_stop

:remove

echo Removing service...
sc delete %SERVICE_NAME%
pause
