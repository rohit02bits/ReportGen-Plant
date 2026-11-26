# Plant Batching Report Generator

This Python application generates daily, shift-wise reports from an SQL Server database, exporting them into beautifully styled Excel spreadsheets that mirror the physical batching reports. It has been designed specifically for Windows environments and can run either as a continuous background service or as discrete runs scheduled via the Windows Task Scheduler.

---

## Features

- **Configurable Layout & Columns:** Excel columns, database fields, display headers, data formatting, and calculation summaries (Averages/Sums) are completely defined in a simple configuration file (`config.yaml`).
- **Flexible Shift Windows:** Supports query offsets to generate reports spanning across calendar days (e.g., Shift C from 10 PM yesterday to 6 AM today).
- **Dual Running Modes:**
  1. **Continuous Daemon Mode:** Runs in the background and uses Python's internal timer scheduler.
  2. **Windows Task Scheduler Mode (Recommended):** The CLI can be called by Task Scheduler using `--run-current` to run whichever shift report is due at that exact moment.
- **OPC DA / OPC UA Live Data Acquisition:** Connects to ABB OPC DA servers (e.g., `ABB.AC800MC_OpcDaServer.3`, `ABB.DriveOPC`) or OPC UA servers, with rising-edge (`LOW → HIGH`) detection on configurable PLC `BOOL` trigger tags (`BatchCompleted` and `BinLevelFull`).
- **Operator Desktop & Web Monitoring UI:** Real-time visual status dashboard (Native Desktop GUI window + Web UI at `http://localhost:8050`) displaying OPC Server communication, PLC BOOL trigger states, database connectivity & live data capturing, and report generation status.
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
   Double-click [run_reporter.bat](file:///Users/rohitkumar/IdeaProjects/ReportGen-Plant/run_reporter.bat) for an interactive menu to initialize databases, populate mock data, run shift reports, launch the Operator Status Monitoring UI, or start background services.

---

## Operator Status & Communication Monitoring UI

The application includes a visual **Operator Status & Communication Monitor** ([monitor_ui.py](file:///Users/rohitkumar/IdeaProjects/ReportGen-Plant/monitor_ui.py)) that runs both as a **Native Desktop Window (Tkinter)** and as a **Live Web Dashboard (`http://localhost:8050`)**.

### 1. How to Launch the Monitoring UI

- **Live Production Mode (OPC DA / OPC UA + Database + Report Scheduler):**
  ```cmd
  python main.py --ui
  ```
- **Simulation / Local Testing Mode (Simulated PLC & Rising-Edge Triggers):**
  ```cmd
  python main.py --ui --opc-sim
  ```
- **Via Windows Batch Menu ([run_reporter.bat](file:///Users/rohitkumar/IdeaProjects/ReportGen-Plant/run_reporter.bat)):**
  Double-click `run_reporter.bat` and enter **`14`** (*Launch Operator Status Monitoring UI*).

Once started:
- **Native Desktop Window:** Opens automatically on the operator PC desktop.
- **Web Browser Dashboard:** Open **http://localhost:8050** in any browser (Chrome, Edge, Safari).
- **Live JSON Telemetry API:** Available at **http://localhost:8050/api/status**.

---

### 2. Visual Status Indicators Explained

The monitoring screen is organized into three real-time status cards plus a live database verification table and communication log:

1. **Card 1 — OPC Server Communication:**
   - **Status Badge:**
     - `● CONNECTED` (Green): Communicating live with the configured OPC Server (`ABB.AC800MC_OpcDaServer.3`).
     - `● CONNECTED (SIMULATION)` (Blue): Running with the built-in PLC simulator.
     - `● CONNECTING` (Amber) / `● DISCONNECTED` or `● ERROR` (Red): Indicates connection retry or OPC server unavailability.
   - **PLC Rising-Edge BOOL Trigger Tag Pills (`BatchCompleted` & `BinLevelFull`):**
     - Displays `○ BatchCompleted: LOW (0)` / `● BatchCompleted: HIGH (1)` and `○ BinLevelFull: LOW (0)` / `● BinLevelFull: HIGH (1)`.
     - Shows the **Last Rising-Edge Trigger** tag name and exact timestamp whenever either tag transitions `LOW → HIGH`.

2. **Card 2 — Database & Data Capturing:**
   - **Status Badge:** `● CAPTURING` (Green), `● CONNECTED` (Green), or `● ERROR` (Red).
   - Displays the active database engine (`SQL Server` or `SQLite`), database target, **Total Stored Records**, **Captured This Session** count, **Latest Captured Batch No.**, **Latest Capture Timestamp**, and **Last Database Write Status**.

3. **Card 3 — Report Generation Status:**
   - **Status Badge:** `● SCHEDULED` / `● READY` / `● SUCCESS` (Green), `● PROCESSING` (Amber), or `● ERROR` (Red).
   - Shows whether the automated daily shift scheduler is active, the **Last Generated Shift**, **Last Report File**, generation timestamp, and number of rows exported.
   - Provides one-click buttons (**`Shift A`**, **`Shift B`**, **`Shift C`**, **`Daily`**) to generate any report immediately.

---

### 3. How to Validate the UI & End-to-End Operation

Follow this checklist to validate all three subsystems from the UI:

#### Step A: Validate OPC Communication & Rising-Edge BOOL Triggers (`BatchCompleted` / `BinLevelFull`)
1. Launch `python main.py --ui --opc-sim` and open **http://localhost:8050** (or view the desktop window).
2. Confirm **Card 1 (OPC Server Communication)** shows `● CONNECTED (SIMULATION)` (or `● CONNECTED` in live mode) and the **Last Poll / Cycle Count** increments every polling cycle.
3. Click **`Pulse BatchCompleted (LOW→HIGH)`** in the top toolbar:
   - Verify the **`BatchCompleted`** indicator pill turns green (`● BatchCompleted: HIGH (1)`).
   - Verify **Last Rising-Edge Trigger** updates to `BatchCompleted` with the current timestamp.
   - Verify a new batch row immediately appears at the top of the **Latest Captured Batch Records in Database** table.
4. Click **`Pulse BinLevelFull (LOW→HIGH)`** in the top toolbar:
   - Verify the **`BinLevelFull`** indicator pill turns green (`● BinLevelFull: HIGH (1)`).
   - Verify **Captured This Session** and **Total Stored Records** in **Card 2** increment by `1`, and the new batch number appears in the live table.

#### Step B: Validate Database Connectivity & Captured Data
1. In **Card 2 (Database & Data Capturing)**, confirm the badge shows `● CAPTURING` and **Last DB Write Status** shows `OK (Batch #<N>)`.
2. Inspect the **Latest Captured Batch Records in Database** table in the lower panel to confirm all bin weights (`WST BQT H (1)`, `QTZ`, `WST BQT H (2)`, `AN COAL`, `WST BQT H (3)`, `HARFER COK`, `WST BQT L`, `WST FBL`) are populated for each captured batch.
3. You can also cross-check from a terminal using:
   ```cmd
   python main.py --db-stats
   sqlite3 -header -column mock_plant.db "SELECT * FROM batching_data ORDER BY batching_time DESC LIMIT 5;"
   ```

#### Step C: Validate Report Generation & Processing
1. In **Card 3 (Report Generation Status)**, click **`Shift A`** (or **`Shift B`**, **`Shift C`**, **`Daily`**).
2. Watch the Card 3 badge transition to `● PROCESSING` and then `● SUCCESS`.
3. Confirm **Last Generated Shift**, **Last Report File** (e.g., `BatchingReport_Shift_A_<YYYYMMDD>.xlsx`), and the exported row count update in Card 3 and in the **Live Communication, Trigger & Report Processing Log** at the bottom of the screen.
4. Click **`Open Reports Folder`** in the desktop toolbar (or check `./reports`) to open the generated `.xlsx` file.

#### Step D: Validate via JSON Status API
You can query the live telemetry endpoint from PowerShell or `curl` to verify automated monitoring:
```cmd
curl http://localhost:8050/api/status
```

---

## PLC Rising-Edge BOOL Trigger Configuration (`config.yaml`)

Under the `opc:` section of [config.yaml](file:///Users/rohitkumar/IdeaProjects/ReportGen-Plant/config.yaml#L168-L181), two configurable PLC `BOOL` trigger tags (`BatchCompleted` and `BinLevelFull`) control automatic database data capture:

```yaml
opc:
  enabled: true
  protocol: "opc_da"
  server: "ABB.AC800MC_OpcDaServer.3"
  host: "localhost"
  polling_interval_seconds: 10

  # Rising-edge trigger mode: writes a new row whenever any configured BOOL tag transitions LOW (0) -> HIGH (1)
  trigger_mode: "trigger_tag"

  trigger_tags:
    batch_completed: "BatchCompleted"
    bin_level_full: "BinLevelFull"
```
- Whenever **either** `batch_completed` **or** `bin_level_full` transitions from `LOW` (`0` / `False`) to `HIGH` (`1` / `True`), the collector captures all mapped bin values at that moment and inserts a new row into `batching_data`.
- If your ABB PLC exposes these tags under an application path (for example `Applications.Application_1.BatchCompleted.Value`), update the tag names under `trigger_tags` in `config.yaml`.

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
