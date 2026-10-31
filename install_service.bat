@echo off
:: Installs the Plant Batching Report Generator as a Windows Service that
:: starts automatically at boot and restarts itself on failure.
:: Must be run as Administrator.

set SERVICE_NAME=PlantReportGenerator
cd /d "%~dp0"

net session >nul 2>&1
if errorlevel 1 (
    echo This script must be run as Administrator.
    echo Right-click install_service.bat and choose "Run as administrator".
    pause
    exit /b 1
)

if exist ReportGeneratorService.exe (
    set RUNNER=ReportGeneratorService.exe
) else (
    set RUNNER=python windows_service.py
)
echo Using runner: %RUNNER%

:: Stop and remove any previous installation
sc query %SERVICE_NAME% >nul 2>&1
if not errorlevel 1 (
    echo Removing existing service...
    %RUNNER% stop >nul 2>&1
    %RUNNER% remove
)

echo Installing service...
%RUNNER% --startup delayed install
if errorlevel 1 (
    echo Service installation failed.
    pause
    exit /b 1
)

:: Restart 60s after each failure; reset the failure counter after 1 day.
:: failureflag 1 also treats a non-zero exit code on stop as a failure.
sc failure %SERVICE_NAME% reset= 86400 actions= restart/60000/restart/60000/restart/60000
sc failureflag %SERVICE_NAME% 1

echo Starting service...
sc start %SERVICE_NAME%

echo.
echo Service "%SERVICE_NAME%" installed (Automatic - Delayed Start, restarts on failure).
echo Logs: %~dp0logs\reporter.log
pause
