@echo off
title Plant Batching Report Generator
cls

:: Auto-detect runner (compiled executable vs. Python script)
set RUNNER=python main.py
if exist ReportGenerator.exe (
    set RUNNER=ReportGenerator.exe
)

:menu
echo ===================================================
echo   PLANT BATCHING REPORT GENERATOR (SQL SERVER/EXCEL)
echo ===================================================
echo.
echo Current Runner: %RUNNER%
echo.
echo Please select an action:
echo  1. Run Shift A Report (6 AM - 2 PM Today)
echo  2. Run Shift B Report (2 PM - 10 PM Today)
echo  3. Run Shift C Report (10 PM Yesterday - 6 AM Today)
echo  4. Run Daily Report (24 hours Yesterday)
echo  5. Run Currently Due Report (Auto-Detect)
echo  6. Start Background Scheduler Service
echo  7. Initialize Mock SQLite Database (Offline Testing)
echo  8. Start OPC DA Data Collector (Real/Production)
echo  9. Start OPC DA Data Collector (Simulation Mode)
echo  10. Exit
echo.
echo ===================================================
set /p choice="Enter choice (1-10): "

if "%choice%"=="1" goto shift_a
if "%choice%"=="2" goto shift_b
if "%choice%"=="3" goto shift_c
if "%choice%"=="4" goto daily
if "%choice%"=="5" goto current
if "%choice%"=="6" goto service
if "%choice%"=="7" goto mock_db
if "%choice%"=="8" goto opc_collect
if "%choice%"=="9" goto opc_sim
if "%choice%"=="10" goto exit
echo Invalid choice. Please try again.
pause
goto menu


:shift_a
echo.
echo Running Shift A Report...
%RUNNER% --run-shift "Shift A"
pause
goto menu

:shift_b
echo.
echo Running Shift B Report...
%RUNNER% --run-shift "Shift B"
pause
goto menu

:shift_c
echo.
echo Running Shift C Report...
%RUNNER% --run-shift "Shift C"
pause
goto menu

:daily
echo.
echo Running Daily Report...
%RUNNER% --run-shift "Daily"
pause
goto menu

:current
echo.
echo Detecting and running currently due report...
%RUNNER% --run-current
pause
goto menu

:service
echo.
echo Starting Background Scheduler Service...
echo Close this window or press Ctrl+C to terminate.
echo.
%RUNNER% --service
pause
goto menu

:mock_db
echo.
echo Initializing Mock SQLite Database...
if exist ReportGenerator.exe (
    echo Note: Using mock_db_setup.py via python. If python is not installed,
    echo run ReportGenerator.exe with mock DB configured in config.yaml.
)
python mock_db_setup.py
pause
goto menu

:opc_collect
echo.
echo Starting OPC DA Data Collector...
echo Close this window or press Ctrl+C to terminate.
echo.
%RUNNER% --collect-opc
pause
goto menu

:opc_sim
echo.
echo Starting OPC DA Data Collector in SIMULATION Mode...
echo Close this window or press Ctrl+C to terminate.
echo.
%RUNNER% --collect-opc --opc-sim
pause
goto menu

:exit
echo.
echo Exiting. Have a nice day!
exit
