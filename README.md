# Plant Batching Report Generator

This Python application generates daily, shift-wise reports from an SQL Server database, exporting them into beautifully styled Excel spreadsheets that mirror the physical batching reports. It has been designed specifically for Windows environments and can run either as a continuous background service or as discrete runs scheduled via the Windows Task Scheduler.

---

## Features

- **Configurable Layout & Columns:** Excel columns, database fields, display headers, data formatting, and calculation summaries (Averages/Sums) are completely defined in a simple configuration file (`config.yaml`).
- **Flexible Shift Windows:** Supports query offsets to generate reports spanning across calendar days (e.g., Shift C from 10 PM yesterday to 6 AM today).
- **Dual Running Modes:**
  1. **Continuous Daemon Mode:** Runs in the background and uses Python's internal timer scheduler.
  2. **Windows Task Scheduler Mode (Recommended):** The CLI can be called by Task Scheduler using `--run-current` to run whichever shift report is due at that exact moment.
- **Offline Mocking:** Features a built-in SQLite fallback and a mock data generator (`mock_db_setup.py`) to verify Excel report formatting without an active SQL Server connection.

---

## Installation & Setup

### 1. Prerequisites
Install [Python 3.8+](https://www.python.org/downloads/) on your Windows machine. Ensure you check the box **"Add Python to PATH"** during installation.

### 2. Project Setup
Clone or copy this folder to your desired location, then open a Command Prompt / PowerShell in this directory and install the python dependencies:

```cmd
pip install -r requirements.txt
```

---

## Database Setup & Mock Data Generation

### 1. SQLite Setup (Offline / Local Testing)
You can set up SQLite in three ways:

- **Via Python Mock Processor (Recommended):**
  ```cmd
  python mock_db_setup.py --sqlite
  ```
  This creates `mock_plant.db`, initializes tables/indexes, and generates realistic batch data for the last 7 days + benchmark date `2026-05-15`.

- **Via SQLite CLI (`.read`):**
  ```cmd
  sqlite3 mock_plant.db
  sqlite> .read schema.sql
  ```
  [schema.sql](file:///Users/rohitkumar/IdeaProjects/ReportGen-Plant/schema.sql) is fully SQLite-compatible with table definitions, index, and sample data.

- **Via Main CLI:**
  ```cmd
  python main.py --init-db
  python main.py --mock-data
  ```

### 2. SQL Server Setup (Production / SSMS)
- **Via SQL Script in SSMS:**
  Open [schema_sqlserver.sql](file:///Users/rohitkumar/IdeaProjects/ReportGen-Plant/schema_sqlserver.sql) in SQL Server Management Studio (SSMS) against your database (e.g., `PlantDB`) and execute.
- **Via Python Processor:**
  ```cmd
  python mock_db_setup.py --sqlserver --init-only
  python mock_db_setup.py --sqlserver --days 7
  ```

### 3. Additional Mock Data Tools
- **Live Stream Batches (Continuous Ingestion Simulation):**
  ```cmd
  python mock_db_setup.py --stream --interval 10
  ```
- **Check Database Stats:**
  ```cmd
  python mock_db_setup.py --stats
  ```
- **Generate Specific Date Range:**
  ```cmd
  python mock_db_setup.py --start-date 2026-05-01 --end-date 2026-05-15
  ```

---

## Testing Reports

1. **Generate a Shift Report:**
   ```cmd
   python main.py --run-shift "Shift A" --date 2026-05-15
   ```
   This queries the database for `2026-05-15 06:00:00` to `2026-05-15 14:00:00` and outputs the Excel report in `./reports`.

2. **Interactive Windows Console Dashboard:**
   Double-click [run_reporter.bat](file:///Users/rohitkumar/IdeaProjects/ReportGen-Plant/run_reporter.bat) for an interactive menu to initialize databases, populate mock data, run shift reports, or start background services.

---

## Configuration (`config.yaml`)

Edit the [config.yaml](config.yaml) file to customize database connections, schedules, and columns:

### 1. Database Connection
- To use SQL Server: Set `fallback_to_sqlite: false`, and enter server name, database name, and credentials. Set `trusted_connection: true` to use Windows Authentication (recommended if running under an AD account).
- To test locally with SQLite: Keep `fallback_to_sqlite: true`.

### 2. Shift Windows and Trigger Timings
The report schedules are configured under the `shifts` section. Every shift contains:
- `run_time`: Trigger time (HH:MM) when the report runs.
- `start_time` / `end_time`: Start and end times of the shift.
- `start_day_offset` / `end_day_offset`: Set to `0` for today, `-1` for yesterday.

For example, **Shift C** (10 PM previous day to 6 AM current day) running at 6:05 AM:
```yaml
- name: "Shift C"
  run_time: "06:05"
  start_time: "22:00:00"
  start_day_offset: -1
  end_time: "06:00:00"
  end_day_offset: 0
```

### 3. Excel Column Mapping
To map columns exactly:
- `excel_col`: The target column letter (allows skipping columns to create blank spacer columns, e.g., using columns B, D, F, H... and leaving C, E, G... empty).
- `db_field`: The database column name returned by the query.
- `excel_header`: Header name written to Row 5.
- `avg_summary: true`: If true, an `AVERAGE` row is automatically generated at the bottom.

---

## Deployment on Windows

### Option A: Using Windows Task Scheduler (Recommended)
This is the most reliable production setup as it survives system restarts, has built-in execution retry logic, and uses 0% CPU/RAM when idle.

1. Open **Task Scheduler** (search in Windows Start Menu).
2. Click **Create Basic Task...** on the right side.
3. Configure the schedule:
   - **Trigger:** Daily.
   - **Start Time:** Select a starting time (e.g., `00:05` for the Daily report).
4. **Action:** Select **Start a Program**.
5. Configure the program:
   - **Program/script:** `python` (or the absolute path to your python.exe, e.g. `C:\Users\<Name>\AppData\Local\Programs\Python\Python310\python.exe`).
   - **Add arguments:** `main.py --run-current` (or specific shifts like `main.py --run-shift "Shift A"`).
   - **Start in:** The absolute path to this folder (e.g. `C:\PlantApplications\ReportGenerator`).
6. Click **Finish**.
7. Create four separate tasks for the daily run times:
   - **Daily Report:** Runs Daily at `12:05 AM` (00:05)
   - **Shift C Report:** Runs Daily at `6:05 AM` (06:05)
   - **Shift A Report:** Runs Daily at `2:05 PM` (14:05)
   - **Shift B Report:** Runs Daily at `10:05 PM` (22:05)

### Option B: Running as a Windows Service (Auto-start & Auto-restart)
The scheduler can be installed as a native Windows Service named `PlantReportGenerator`. It starts automatically at boot (Automatic - Delayed Start) and Windows restarts it 60 seconds after any crash.

#### 1. Get the service executable
Either download the **ReportGenerator-Windows-Deployment** artifact from the latest *Build Windows Executable* run in GitHub Actions, or build it locally:

```cmd
pip install -r requirements.txt
python package.py
```
This creates `dist\ReportGenerator.exe` and `dist\ReportGeneratorService.exe`.

#### 2. Install
1. Copy `ReportGeneratorService.exe`, `config.yaml`, `install_service.bat` and `uninstall_service.bat` into one folder at its final location, e.g. `C:\PlantApplications\ReportGenerator`.
2. Right-click `install_service.bat` and choose **Run as administrator**.

#### 3. Verify
- **Optional, before installing:** run the service in the console to check config and schedules (Ctrl+C to stop):
  ```cmd
  ReportGeneratorService.exe debug
  ```
  You should see `Scheduling 'Shift A' to run daily at 14:05` (and the other shifts) followed by `Scheduler service is running`.
- **Service status and settings** (from an Administrator PowerShell):
  ```powershell
  sc.exe query PlantReportGenerator      # STATE should be RUNNING
  sc.exe qc PlantReportGenerator         # START_TYPE should be AUTO_START (DELAYED)
  sc.exe qfailure PlantReportGenerator   # FAILURE_ACTIONS should be RESTART -- Delay = 60000 ms
  ```
- **Auto-restart after a crash:** note the PID, kill the process, wait 70 seconds and check that it is `RUNNING` again with a new PID:
  ```powershell
  sc.exe queryex PlantReportGenerator
  Stop-Process -Name ReportGeneratorService -Force
  Start-Sleep 70
  sc.exe queryex PlantReportGenerator
  ```
  `logs\reporter.log` will show a new `Service starting from: ...` line for the restart.

#### 4. Manage
Use `services.msc` or the command line:

```cmd
sc query PlantReportGenerator
sc stop PlantReportGenerator
sc start PlantReportGenerator
```

To remove it, run `uninstall_service.bat` as administrator.

**Notes:**
- Logs are written to `logs\reporter.log` next to the executable; service start/stop events also appear in the Windows Event Viewer (Application log).
- The service runs as `LocalSystem` by default. If SQL Server uses Windows Authentication (`trusted_connection: true`), set the service's **Log On** account in `services.msc` to a user that has database access.
- Without the compiled executable, `install_service.bat` falls back to `python windows_service.py`. This needs `pywin32` installed for the system-wide Python and `python -m pywin32_postinstall -install` run once as administrator.

### Option C: Running as a Continuous Background Daemon
Alternatively, you can run the scheduler continuously in a command window:

```cmd
python main.py --service
```
This reads the shift `run_time` values from `config.yaml` and executes the queries at those specific times.
