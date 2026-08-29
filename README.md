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

## Testing Offline (No SQL Server Connection Required)

To verify the report formats and schedules instantly:

1. **Initialize the Mock SQLite database:**
   ```cmd
   python mock_db_setup.py
   ```
   This creates a local database file `mock_plant.db` and populates it with realistic batch records, including records on the historical date `2026-05-15` (to replicate the exact data in the sample screenshot).

2. **Generate a Shift Report:**
   ```cmd
   python main.py --run-shift "Shift A" --date 2026-05-15
   ```
   This will query the database for the window `2026-05-15 06:00:00` to `2026-05-15 14:00:00` and generate the report inside the `./reports` directory.

3. **Check the Interactive Menu:**
   Double-click `run_reporter.bat` to launch the console dashboard menu where you can generate shift reports, start the service, or initialize the mock DB.

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

### Option B: Running as a Continuous Background Daemon
Alternatively, you can run the scheduler continuously in a command window or as a custom Windows Service using a process manager:

```cmd
python main.py --service
```
This reads the shift `run_time` values from `config.yaml` and executes the queries at those specific times.
