import os
import sys
import json
import time
import logging
import threading
import subprocess
from http.server import HTTPServer, BaseHTTPRequestHandler
from datetime import datetime, date

logger = logging.getLogger("ReportGenerator.MonitorUI")


class SystemStatusTracker:
    """
    Thread-safe and process-safe status tracker for monitoring:
    1. OPC Server communication & PLC BOOL trigger tag states (BatchCompleted, BinLevelFull)
    2. Database connectivity & real-time data capturing status
    3. Report generation & scheduled processing status

    Persists state to `logs/system_status.json` so both in-process threads and
    external CLI processes (`--collect-opc`, `--service`, `--run-shift`) are reflected in the UI.
    """

    def __init__(self, base_dir=None):
        if base_dir is None:
            if getattr(sys, "frozen", False):
                base_dir = os.path.dirname(sys.executable)
            else:
                base_dir = os.path.dirname(os.path.abspath(__file__))
        self.base_dir = base_dir
        self.log_dir = os.path.join(self.base_dir, "logs")
        os.makedirs(self.log_dir, exist_ok=True)
        self.status_file = os.path.join(self.log_dir, "system_status.json")
        self._lock = threading.Lock()
        self.state = self._default_state()
        self._load_existing()

    @staticmethod
    def _default_state():
        return {
            "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "opc": {
                "state": "DISCONNECTED",  # CONNECTED, SIMULATION, CONNECTING, ERROR, DISCONNECTED
                "collector_running": False,
                "protocol": "-",
                "server": "-",
                "host": "localhost",
                "trigger_mode": "trigger_tag",
                "trigger_tags": {
                    "BatchCompleted": 0,
                    "BinLevelFull": 0,
                },
                "last_triggered_by": "-",
                "last_trigger_time": "-",
                "last_poll_time": "-",
                "poll_count": 0,
                "latest_values": {},
                "message": "OPC Collector is not running",
            },
            "database": {
                "state": "UNKNOWN",  # CONNECTED, CAPTURING, FALLBACK_SQLITE, ERROR, UNKNOWN
                "engine": "-",
                "target": "-",
                "total_records": 0,
                "session_captured": 0,
                "last_batch_no": "-",
                "last_batch_time": "-",
                "last_write_time": "-",
                "last_write_status": "Ready",
                "message": "Checking database connection...",
            },
            "reporting": {
                "state": "IDLE",  # IDLE, SCHEDULED, PROCESSING, SUCCESS, ERROR
                "scheduler_running": False,
                "active_shift": "-",
                "last_report_shift": "-",
                "last_report_file": "-",
                "last_report_time": "-",
                "last_report_rows": 0,
                "next_scheduled": "-",
                "message": "Ready to generate reports",
            },
        }

    def _load_existing(self):
        with self._lock:
            if os.path.exists(self.status_file):
                try:
                    with open(self.status_file, "r", encoding="utf-8") as f:
                        loaded = json.load(f)
                    if isinstance(loaded, dict):
                        for section in ("opc", "database", "reporting"):
                            if section in loaded and isinstance(loaded[section], dict):
                                self.state[section].update(loaded[section])
                except Exception:
                    pass

    def _save_locked(self):
        self.state["updated_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        tmp_path = self.status_file + ".tmp"
        try:
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(self.state, f, indent=2, default=str)
            os.replace(tmp_path, self.status_file)
        except Exception:
            pass

    def reload_from_disk(self):
        self._load_existing()
        return self.get_snapshot()

    def get_snapshot(self):
        with self._lock:
            return json.loads(json.dumps(self.state, default=str))

    def update_opc(self, **kwargs):
        with self._lock:
            self.state["opc"].update(kwargs)
            self._save_locked()

    def update_database(self, **kwargs):
        with self._lock:
            self.state["database"].update(kwargs)
            self._save_locked()

    def update_reporting(self, **kwargs):
        with self._lock:
            self.state["reporting"].update(kwargs)
            self._save_locked()

    def record_db_capture(self, batch_no, batch_time_str, engine_label=None):
        with self._lock:
            db = self.state["database"]
            db["state"] = "CAPTURING"
            db["session_captured"] = int(db.get("session_captured", 0) or 0) + 1
            db["total_records"] = int(db.get("total_records", 0) or 0) + 1
            db["last_batch_no"] = batch_no
            db["last_batch_time"] = batch_time_str
            db["last_write_time"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            db["last_write_status"] = f"OK (Batch #{batch_no})"
            db["message"] = f"Captured Batch #{batch_no} at {batch_time_str}"
            if engine_label:
                db["engine"] = engine_label
            self._save_locked()


# Global tracker singleton
_GLOBAL_TRACKER = None


def get_status_tracker(base_dir=None):
    global _GLOBAL_TRACKER
    if _GLOBAL_TRACKER is None:
        _GLOBAL_TRACKER = SystemStatusTracker(base_dir)
    return _GLOBAL_TRACKER


class UILogHandler(logging.Handler):
    """Forwards Python logging records into a thread-safe ring buffer for the Operator UI."""

    def __init__(self, max_entries=250):
        super().__init__()
        self.max_entries = max_entries
        self.entries = []
        self._lock = threading.Lock()
        self.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s", "%H:%M:%S"))

    def emit(self, record):
        try:
            msg = self.format(record)
            with self._lock:
                self.entries.append((record.levelname, msg))
                if len(self.entries) > self.max_entries:
                    self.entries = self.entries[-self.max_entries:]
        except Exception:
            pass

    def drain_new(self, last_index):
        with self._lock:
            total = len(self.entries)
            if last_index >= total:
                return [], total
            return list(self.entries[last_index:]), total


class OperatorMonitorUI:
    """
    Desktop Operator Monitoring Dashboard (Tkinter) showing real-time visual indicators for:
    1. OPC Server Communication & PLC BOOL Trigger Tags (BatchCompleted, BinLevelFull)
    2. Database Connectivity & Data Capturing Status
    3. Report Generation & Scheduled Processing Status
    """

    COLORS = {
        "bg": "#0f172a",
        "card_bg": "#1e293b",
        "card_border": "#334155",
        "header_bg": "#020617",
        "text": "#f8fafc",
        "muted": "#94a3b8",
        "green_bg": "#065f46",
        "green_fg": "#34d399",
        "blue_bg": "#1e3a8a",
        "blue_fg": "#60a5fa",
        "amber_bg": "#78350f",
        "amber_fg": "#fbbf24",
        "red_bg": "#7f1d1d",
        "red_fg": "#f87171",
        "gray_bg": "#334155",
        "gray_fg": "#cbd5e1",
        "btn_primary": "#2563eb",
        "btn_green": "#059669",
        "btn_amber": "#d97706",
        "btn_red": "#dc2626",
        "btn_slate": "#334155",
    }

    def __init__(self, config, auto_start_services=True, force_opc_sim=False):
        self.config = config
        self.auto_start_services = auto_start_services
        self.force_opc_sim = force_opc_sim
        self.tracker = get_status_tracker()

        from db_client import DatabaseClient
        from scheduler import Scheduler

        self.db_client = DatabaseClient(config)
        self.scheduler = Scheduler(config)

        # Managed background worker threads
        self.opc_collector = None
        self.opc_thread = None
        self.scheduler_stop_event = threading.Event()
        self.scheduler_thread = None

        # Logging capture
        self.ui_log_handler = UILogHandler()
        logging.getLogger().addHandler(self.ui_log_handler)
        self._log_cursor = 0

        # Initialize basic static info into tracker
        opc_cfg = config.get("opc", {})
        self.tracker.update_opc(
            server=opc_cfg.get("server", "ABB.AC800MC_OpcDaServer.3"),
            host=opc_cfg.get("host", "localhost"),
            trigger_mode=opc_cfg.get("trigger_mode", "trigger_tag"),
        )

    def probe_database_status(self):
        """Probes the configured database and returns recent rows along with updated tracker state."""
        recent_rows = []
        try:
            stats = self.db_client.get_stats()
            if "error" in stats:
                self.tracker.update_database(
                    state="ERROR",
                    engine="SQLite" if self.db_client.use_sqlite else "SQL Server",
                    target=self.db_client.sqlite_path if self.db_client.use_sqlite else self.db_client.db_config.get("server", "localhost"),
                    last_write_status="Connection Error",
                    message=str(stats["error"]),
                )
                return recent_rows

            engine = stats.get("engine", "SQLite" if self.db_client.use_sqlite else "SQL Server")
            target = (
                os.path.basename(self.db_client.sqlite_path)
                if self.db_client.use_sqlite
                else f"{self.db_client.db_config.get('server', 'localhost')} / {self.db_client.db_config.get('database', 'PlantDB')}"
            )
            current_db_state = self.tracker.get_snapshot()["database"].get("state", "UNKNOWN")
            new_state = "CAPTURING" if (self.opc_collector and self.opc_collector.running) or current_db_state == "CAPTURING" else "CONNECTED"

            self.tracker.update_database(
                state=new_state,
                engine=engine,
                target=target,
                total_records=stats.get("count", 0),
                last_batch_no=stats.get("max_batch") if stats.get("max_batch") is not None else "-",
                last_batch_time=stats.get("max_time") if stats.get("max_time") is not None else "-",
                message=f"Connected to {engine} ({target}) — {stats.get('count', 0)} total records",
            )

            # Fetch latest 12 rows for the live table
            conn = self.db_client.get_connection()
            try:
                table_name = "batching_data" if self.db_client.use_sqlite else "[dbo].[batching_data]"
                cursor = conn.cursor()
                if self.db_client.use_sqlite:
                    cursor.execute(
                        f"SELECT batch_no, batching_time, wst_bqt_h_1, qtz, wst_bqt_h_2, an_coal, "
                        f"wst_bqt_h_3, harfer_cok, wst_bqt_l, wst_fbl "
                        f"FROM {table_name} ORDER BY batching_time DESC, batch_no DESC LIMIT 12"
                    )
                else:
                    cursor.execute(
                        f"SELECT TOP 12 batch_no, batching_time, wst_bqt_h_1, qtz, wst_bqt_h_2, an_coal, "
                        f"wst_bqt_h_3, harfer_cok, wst_bqt_l, wst_fbl "
                        f"FROM {table_name} ORDER BY batching_time DESC, batch_no DESC"
                    )
                recent_rows = cursor.fetchall()
            finally:
                conn.close()
        except Exception as e:
            self.tracker.update_database(
                state="ERROR",
                last_write_status="Failed",
                message=f"Database error: {e}",
            )
        return recent_rows

    # =========================================================================
    # Background Services Control (OPC Collector & Scheduler)
    # =========================================================================

    def start_opc_collector(self, simulation=False):
        if self.opc_collector and self.opc_collector.running:
            logger.info("OPC Collector is already running.")
            return

        from opc_collector import OPCCollector

        self.opc_collector = OPCCollector(self.config, force_simulation=simulation)

        def _run_collector():
            try:
                self.opc_collector.start()
            except Exception as e:
                logger.error(f"OPC Collector thread exited with error: {e}")
                self.tracker.update_opc(
                    state="ERROR",
                    collector_running=False,
                    message=f"Stopped: {e}",
                )

        self.opc_thread = threading.Thread(target=_run_collector, name="OPCCollectorThread", daemon=True)
        self.opc_thread.start()

    def stop_opc_collector(self):
        if self.opc_collector and self.opc_collector.running:
            self.opc_collector.stop()
            self.tracker.update_opc(
                state="DISCONNECTED",
                collector_running=False,
                message="OPC Collector stopped by operator",
            )

    def start_scheduler_service(self):
        if self.scheduler_thread and self.scheduler_thread.is_alive():
            logger.info("Report Scheduler service is already running.")
            return

        self.scheduler_stop_event.clear()
        shifts_summary = ", ".join(f"{s['name']} @ {s.get('run_time', '00:00')}" for s in self.config.get("shifts", []))

        def _run_scheduler():
            self.tracker.update_reporting(
                state="SCHEDULED",
                scheduler_running=True,
                next_scheduled=shifts_summary,
                message="Automated Shift Report Scheduler is active",
            )
            try:
                self.scheduler.start_service_mode(dry_run=False, stop_event=self.scheduler_stop_event)
            except Exception as e:
                self.tracker.update_reporting(
                    state="ERROR",
                    scheduler_running=False,
                    message=f"Scheduler error: {e}",
                )
            finally:
                self.tracker.update_reporting(scheduler_running=False)

        self.scheduler_thread = threading.Thread(target=_run_scheduler, name="SchedulerThread", daemon=True)
        self.scheduler_thread.start()

    def stop_scheduler_service(self):
        self.scheduler_stop_event.set()
        self.tracker.update_reporting(
            state="IDLE",
            scheduler_running=False,
            message="Scheduler stopped by operator",
        )

    def trigger_manual_report(self, shift_name):
        def _job():
            try:
                self.scheduler.run_shift_report(shift_name, date.today())
            except Exception as e:
                logger.error(f"Manual report generation failed for '{shift_name}': {e}")

        threading.Thread(target=_job, name=f"ManualReport-{shift_name}", daemon=True).start()

    def trigger_simulated_rising_edge(self, trigger_tag_name="BatchCompleted"):
        """Simulates an immediate LOW -> HIGH rising edge trigger to test DB data capture from the UI."""
        def _job():
            try:
                from opc_collector import OPCCollector
                collector = self.opc_collector or OPCCollector(self.config, force_simulation=True)
                if collector.last_batch_no is None:
                    collector._initialize_db_state()
                sim_tags = collector.opc_client._generate_simulated_tags(trigger_high=False)
                # Ensure baseline LOW then rising-edge HIGH on requested tag
                collector.last_trigger_vals[trigger_tag_name] = 0
                sim_tags[trigger_tag_name] = True
                collector._process_trigger_tag(sim_tags)
                self.tracker.update_opc(
                    trigger_tags={t: (1 if t == trigger_tag_name else 0) for t in collector.trigger_tags},
                    last_triggered_by=trigger_tag_name,
                    last_trigger_time=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    latest_values=sim_tags,
                )
            except Exception as e:
                logger.error(f"Test trigger failed: {e}")

        threading.Thread(target=_job, name="ManualTriggerTest", daemon=True).start()

    def open_reports_folder(self):
        out_dir = self.config.get("reporting", {}).get("output_dir", "./reports")
        if not os.path.isabs(out_dir):
            out_dir = os.path.join(self.tracker.base_dir, out_dir)
        os.makedirs(out_dir, exist_ok=True)
        try:
            if sys.platform == "win32":
                os.startfile(out_dir)
            elif sys.platform == "darwin":
                subprocess.Popen(["open", out_dir])
            else:
                subprocess.Popen(["xdg-open", out_dir])
        except Exception as e:
            logger.warning(f"Could not open reports folder '{out_dir}': {e}")

    # =========================================================================
    # Tkinter Desktop GUI Construction
    # =========================================================================

    def launch(self):
        import tkinter as tk
        from tkinter import ttk

        self.root = tk.Tk()
        self.root.title("Plant Batching System — Operator Communication & Status Monitor")
        self.root.geometry("1280x820")
        self.root.minsize(1100, 720)
        self.root.configure(bg=self.COLORS["bg"])

        self._build_layout(tk, ttk)

        # Initial probe
        self.probe_database_status()
        self.start_web_server(port=8050)

        if self.auto_start_services:
            self.start_scheduler_service()
            if self.config.get("opc", {}).get("enabled", True):
                self.start_opc_collector(simulation=self.force_opc_sim)

        self._schedule_ui_refresh()
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self.root.mainloop()

    def _on_close(self):
        try:
            self.stop_opc_collector()
            self.stop_scheduler_service()
        except Exception:
            pass
        self.root.destroy()

    def _badge_style_for_state(self, state_str):
        s = str(state_str or "").upper()
        if s in ("CONNECTED", "CAPTURING", "SCHEDULED", "SUCCESS", "READY", "HIGH"):
            return self.COLORS["green_bg"], self.COLORS["green_fg"], f"● {s}"
        if s in ("SIMULATION",):
            return self.COLORS["blue_bg"], self.COLORS["blue_fg"], "● CONNECTED (SIMULATION)"
        if s in ("CONNECTING", "PROCESSING", "FALLBACK_SQLITE", "IDLE"):
            return self.COLORS["amber_bg"], self.COLORS["amber_fg"], f"● {s}"
        if s in ("ERROR", "FAILED", "DISCONNECTED"):
            return self.COLORS["red_bg"], self.COLORS["red_fg"], f"● {s}"
        return self.COLORS["gray_bg"], self.COLORS["gray_fg"], f"● {s or 'STANDBY'}"

    def _build_layout(self, tk, ttk):
        c = self.COLORS

        # 1. Header Banner
        header = tk.Frame(self.root, bg=c["header_bg"], padx=20, pady=12)
        header.pack(fill="x")

        title_box = tk.Frame(header, bg=c["header_bg"])
        title_box.pack(side="left")
        tk.Label(
            title_box,
            text="PLANT BATCHING SYSTEM — OPERATOR STATUS MONITOR",
            font=("Helvetica", 15, "bold"),
            fg=c["text"],
            bg=c["header_bg"],
        ).pack(anchor="w")
        tk.Label(
            title_box,
            text="Live Software Communication, PLC BOOL Trigger Tracking, Database Capture & Excel Reporting Indication",
            font=("Helvetica", 10),
            fg=c["muted"],
            bg=c["header_bg"],
        ).pack(anchor="w")

        right_hdr = tk.Frame(header, bg=c["header_bg"])
        right_hdr.pack(side="right")
        self.overall_badge = tk.Label(
            right_hdr,
            text="● SYSTEM INITIALIZING",
            font=("Helvetica", 11, "bold"),
            padx=14,
            pady=6,
            bg=c["amber_bg"],
            fg=c["amber_fg"],
        )
        self.overall_badge.pack(side="right", padx=(12, 0))
        self.clock_label = tk.Label(
            right_hdr,
            text="",
            font=("Helvetica", 11),
            fg=c["muted"],
            bg=c["header_bg"],
        )
        self.clock_label.pack(side="right")

        # 2. Operator Control Toolbar
        toolbar = tk.Frame(self.root, bg=c["card_bg"], padx=16, pady=8, highlightthickness=1, highlightbackground=c["card_border"])
        toolbar.pack(fill="x", padx=16, pady=(10, 6))

        tk.Label(toolbar, text="OPC Controls:", font=("Helvetica", 10, "bold"), fg=c["text"], bg=c["card_bg"]).pack(side="left", padx=(0, 6))
        tk.Button(
            toolbar, text="Start OPC (Live)", command=lambda: self.start_opc_collector(simulation=False),
            bg=c["btn_green"], fg="white", relief="flat", padx=10, pady=4, font=("Helvetica", 9, "bold")
        ).pack(side="left", padx=3)
        tk.Button(
            toolbar, text="Start OPC (Sim)", command=lambda: self.start_opc_collector(simulation=True),
            bg=c["btn_primary"], fg="white", relief="flat", padx=10, pady=4, font=("Helvetica", 9, "bold")
        ).pack(side="left", padx=3)
        tk.Button(
            toolbar, text="Stop OPC", command=self.stop_opc_collector,
            bg=c["btn_red"], fg="white", relief="flat", padx=10, pady=4, font=("Helvetica", 9, "bold")
        ).pack(side="left", padx=(3, 14))

        tk.Label(toolbar, text="Test Rising-Edge Trigger:", font=("Helvetica", 10, "bold"), fg=c["text"], bg=c["card_bg"]).pack(side="left", padx=(0, 6))
        tk.Button(
            toolbar, text="Pulse BatchCompleted (LOW→HIGH)", command=lambda: self.trigger_simulated_rising_edge("BatchCompleted"),
            bg=c["btn_amber"], fg="white", relief="flat", padx=10, pady=4, font=("Helvetica", 9, "bold")
        ).pack(side="left", padx=3)
        tk.Button(
            toolbar, text="Pulse BinLevelFull (LOW→HIGH)", command=lambda: self.trigger_simulated_rising_edge("BinLevelFull"),
            bg=c["btn_amber"], fg="white", relief="flat", padx=10, pady=4, font=("Helvetica", 9, "bold")
        ).pack(side="left", padx=(3, 14))

        tk.Button(
            toolbar, text="Open Reports Folder", command=self.open_reports_folder,
            bg=c["btn_slate"], fg="white", relief="flat", padx=10, pady=4, font=("Helvetica", 9, "bold")
        ).pack(side="right", padx=3)

        # 3. Three Core Visual Status Indication Cards
        cards_frame = tk.Frame(self.root, bg=c["bg"])
        cards_frame.pack(fill="x", padx=16, pady=6)
        cards_frame.columnconfigure(0, weight=1, uniform="card")
        cards_frame.columnconfigure(1, weight=1, uniform="card")
        cards_frame.columnconfigure(2, weight=1, uniform="card")

        self._build_opc_card(tk, cards_frame)
        self._build_db_card(tk, cards_frame)
        self._build_report_card(tk, cards_frame)

        # 4. Lower Split: Recent Captured Database Batches Table + Live Event Log
        bottom_pane = tk.Frame(self.root, bg=c["bg"])
        bottom_pane.pack(fill="both", expand=True, padx=16, pady=(6, 14))
        bottom_pane.rowconfigure(0, weight=3)
        bottom_pane.rowconfigure(1, weight=2)
        bottom_pane.columnconfigure(0, weight=1)

        # 4A. Recent Captured Database Batches Table
        table_card = tk.Frame(bottom_pane, bg=c["card_bg"], padx=14, pady=10, highlightthickness=1, highlightbackground=c["card_border"])
        table_card.grid(row=0, column=0, sticky="nsew", pady=(0, 8))

        tbl_hdr = tk.Frame(table_card, bg=c["card_bg"])
        tbl_hdr.pack(fill="x", pady=(0, 6))
        tk.Label(
            tbl_hdr,
            text="LATEST CAPTURED BATCH RECORDS IN DATABASE (LIVE VERIFICATION)",
            font=("Helvetica", 11, "bold"),
            fg=c["text"],
            bg=c["card_bg"],
        ).pack(side="left")
        self.table_summary_lbl = tk.Label(
            tbl_hdr,
            text="",
            font=("Helvetica", 10),
            fg=c["muted"],
            bg=c["card_bg"],
        )
        self.table_summary_lbl.pack(side="right")

        style = ttk.Style()
        try:
            style.theme_use("clam")
        except Exception:
            pass
        style.configure(
            "Batch.Treeview",
            background="#0f172a",
            foreground="#f8fafc",
            fieldbackground="#0f172a",
            rowheight=24,
            font=("Helvetica", 10),
        )
        style.configure(
            "Batch.Treeview.Heading",
            background="#334155",
            foreground="#f8fafc",
            font=("Helvetica", 10, "bold"),
        )

        cols = (
            "batch_no", "batching_time", "wst_bqt_h_1", "qtz", "wst_bqt_h_2",
            "an_coal", "wst_bqt_h_3", "harfer_cok", "wst_bqt_l", "wst_fbl"
        )
        headers = {
            "batch_no": "BATCH NO.",
            "batching_time": "BATCHING TIME",
            "wst_bqt_h_1": "WST BQT H (1)",
            "qtz": "QTZ",
            "wst_bqt_h_2": "WST BQT H (2)",
            "an_coal": "AN COAL",
            "wst_bqt_h_3": "WST BQT H (3)",
            "harfer_cok": "HARFER COK",
            "wst_bqt_l": "WST BQT L",
            "wst_fbl": "WST FBL",
        }
        self.tree = ttk.Treeview(table_card, columns=cols, show="headings", height=7, style="Batch.Treeview")
        for col_id in cols:
            self.tree.heading(col_id, text=headers[col_id])
            width = 145 if col_id == "batching_time" else 95
            self.tree.column(col_id, width=width, anchor="center")
        self.tree.pack(fill="both", expand=True)

        # 4B. Live Activity Log
        log_card = tk.Frame(bottom_pane, bg=c["card_bg"], padx=14, pady=10, highlightthickness=1, highlightbackground=c["card_border"])
        log_card.grid(row=1, column=0, sticky="nsew")

        tk.Label(
            log_card,
            text="LIVE COMMUNICATION, TRIGGER & REPORT PROCESSING LOG",
            font=("Helvetica", 10, "bold"),
            fg=c["text"],
            bg=c["card_bg"],
        ).pack(anchor="w", pady=(0, 4))

        self.log_text = tk.Text(
            log_card,
            height=6,
            bg="#020617",
            fg="#e2e8f0",
            font=("Courier", 10),
            relief="flat",
            state="disabled",
        )
        self.log_text.pack(fill="both", expand=True)
        self.log_text.tag_config("INFO", foreground="#cbd5e1")
        self.log_text.tag_config("WARNING", foreground="#fbbf24")
        self.log_text.tag_config("ERROR", foreground="#f87171")
        self.log_text.tag_config("CRITICAL", foreground="#f87171")

    def _create_kv_row(self, tk, parent, label_text):
        c = self.COLORS
        row = tk.Frame(parent, bg=c["card_bg"])
        row.pack(fill="x", pady=2)
        tk.Label(row, text=label_text, font=("Helvetica", 10), fg=c["muted"], bg=c["card_bg"]).pack(side="left")
        val_lbl = tk.Label(row, text="-", font=("Helvetica", 10, "bold"), fg=c["text"], bg=c["card_bg"])
        val_lbl.pack(side="right")
        return val_lbl

    def _build_opc_card(self, tk, parent):
        c = self.COLORS
        card = tk.Frame(parent, bg=c["card_bg"], padx=14, pady=12, highlightthickness=1, highlightbackground=c["card_border"])
        card.grid(row=0, column=0, sticky="nsew", padx=(0, 6))

        hdr = tk.Frame(card, bg=c["card_bg"])
        hdr.pack(fill="x", pady=(0, 8))
        tk.Label(hdr, text="1. OPC SERVER COMMUNICATION", font=("Helvetica", 11, "bold"), fg=c["text"], bg=c["card_bg"]).pack(side="left")
        self.opc_badge = tk.Label(hdr, text="● DISCONNECTED", font=("Helvetica", 10, "bold"), padx=10, pady=3, bg=c["red_bg"], fg=c["red_fg"])
        self.opc_badge.pack(side="right")

        self.opc_server_lbl = self._create_kv_row(tk, card, "OPC Server / Host:")
        self.opc_proto_lbl = self._create_kv_row(tk, card, "Active Protocol / Driver:")
        self.opc_poll_lbl = self._create_kv_row(tk, card, "Last Poll / Cycle Count:")
        self.opc_last_trig_lbl = self._create_kv_row(tk, card, "Last Rising-Edge Trigger:")

        # PLC BOOL Trigger Tag Indicators (BatchCompleted & BinLevelFull)
        trig_box = tk.Frame(card, bg="#0f172a", padx=10, pady=8, highlightthickness=1, highlightbackground=c["card_border"])
        trig_box.pack(fill="x", pady=(8, 6))
        tk.Label(trig_box, text="PLC Rising-Edge BOOL Trigger Tags (LOW → HIGH):", font=("Helvetica", 9, "bold"), fg=c["muted"], bg="#0f172a").pack(anchor="w", pady=(0, 4))

        pills_row = tk.Frame(trig_box, bg="#0f172a")
        pills_row.pack(fill="x")
        self.batch_completed_pill = tk.Label(
            pills_row, text="BatchCompleted: LOW (0)", font=("Helvetica", 9, "bold"),
            padx=8, pady=4, bg=c["gray_bg"], fg=c["gray_fg"]
        )
        self.batch_completed_pill.pack(side="left", expand=True, fill="x", padx=(0, 4))

        self.bin_level_full_pill = tk.Label(
            pills_row, text="BinLevelFull: LOW (0)", font=("Helvetica", 9, "bold"),
            padx=8, pady=4, bg=c["gray_bg"], fg=c["gray_fg"]
        )
        self.bin_level_full_pill.pack(side="left", expand=True, fill="x", padx=(4, 0))

        self.opc_msg_lbl = tk.Label(card, text="Waiting for OPC connection...", font=("Helvetica", 9), fg=c["muted"], bg=c["card_bg"], wraplength=360, justify="left")
        self.opc_msg_lbl.pack(anchor="w", pady=(4, 0))

    def _build_db_card(self, tk, parent):
        c = self.COLORS
        card = tk.Frame(parent, bg=c["card_bg"], padx=14, pady=12, highlightthickness=1, highlightbackground=c["card_border"])
        card.grid(row=0, column=1, sticky="nsew", padx=6)

        hdr = tk.Frame(card, bg=c["card_bg"])
        hdr.pack(fill="x", pady=(0, 8))
        tk.Label(hdr, text="2. DATABASE & DATA CAPTURING", font=("Helvetica", 11, "bold"), fg=c["text"], bg=c["card_bg"]).pack(side="left")
        self.db_badge = tk.Label(hdr, text="● CHECKING...", font=("Helvetica", 10, "bold"), padx=10, pady=3, bg=c["amber_bg"], fg=c["amber_fg"])
        self.db_badge.pack(side="right")

        self.db_engine_lbl = self._create_kv_row(tk, card, "Database Engine / Target:")
        self.db_total_lbl = self._create_kv_row(tk, card, "Total Stored Records:")
        self.db_session_lbl = self._create_kv_row(tk, card, "Captured This Session:")
        self.db_latest_batch_lbl = self._create_kv_row(tk, card, "Latest Captured Batch No:")
        self.db_latest_time_lbl = self._create_kv_row(tk, card, "Latest Capture Timestamp:")
        self.db_write_status_lbl = self._create_kv_row(tk, card, "Last Database Write Status:")

        self.db_msg_lbl = tk.Label(card, text="Checking database connectivity...", font=("Helvetica", 9), fg=c["muted"], bg=c["card_bg"], wraplength=360, justify="left")
        self.db_msg_lbl.pack(anchor="w", pady=(10, 0))

    def _build_report_card(self, tk, parent):
        c = self.COLORS
        card = tk.Frame(parent, bg=c["card_bg"], padx=14, pady=12, highlightthickness=1, highlightbackground=c["card_border"])
        card.grid(row=0, column=2, sticky="nsew", padx=(6, 0))

        hdr = tk.Frame(card, bg=c["card_bg"])
        hdr.pack(fill="x", pady=(0, 8))
        tk.Label(hdr, text="3. REPORT GENERATION STATUS", font=("Helvetica", 11, "bold"), fg=c["text"], bg=c["card_bg"]).pack(side="left")
        self.rep_badge = tk.Label(hdr, text="● READY", font=("Helvetica", 10, "bold"), padx=10, pady=3, bg=c["green_bg"], fg=c["green_fg"])
        self.rep_badge.pack(side="right")

        self.rep_sched_lbl = self._create_kv_row(tk, card, "Scheduler Service:")
        self.rep_last_shift_lbl = self._create_kv_row(tk, card, "Last Generated Shift:")
        self.rep_last_file_lbl = self._create_kv_row(tk, card, "Last Report File:")
        self.rep_last_time_lbl = self._create_kv_row(tk, card, "Last Generated At / Rows:")

        btn_box = tk.Frame(card, bg=c["card_bg"])
        btn_box.pack(fill="x", pady=(8, 4))
        tk.Label(btn_box, text="Generate Report Immediately:", font=("Helvetica", 9, "bold"), fg=c["muted"], bg=c["card_bg"]).pack(anchor="w", pady=(0, 4))

        btns_row = tk.Frame(btn_box, bg=c["card_bg"])
        btns_row.pack(fill="x")
        for s_name in ("Shift A", "Shift B", "Shift C", "Daily"):
            tk.Button(
                btns_row,
                text=s_name,
                command=lambda n=s_name: self.trigger_manual_report(n),
                bg=c["btn_primary"],
                fg="white",
                relief="flat",
                padx=8,
                pady=4,
                font=("Helvetica", 9, "bold"),
            ).pack(side="left", expand=True, fill="x", padx=2)

        self.rep_msg_lbl = tk.Label(card, text="Ready to generate Excel shift reports.", font=("Helvetica", 9), fg=c["muted"], bg=c["card_bg"], wraplength=360, justify="left")
        self.rep_msg_lbl.pack(anchor="w", pady=(4, 0))

    # =========================================================================
    # Periodic UI Refresh Loop
    # =========================================================================

    def _schedule_ui_refresh(self):
        self._refresh_ui()
        self.root.after(1500, self._schedule_ui_refresh)

    def _refresh_ui(self):
        c = self.COLORS
        self.clock_label.configure(text=datetime.now().strftime("%Y-%m-%d  %H:%M:%S"))

        # Reload shared status from disk (in case external CLI processes updated it) and probe DB
        recent_rows = self.probe_database_status()
        snap = self.tracker.reload_from_disk()
        opc = snap.get("opc", {})
        db = snap.get("database", {})
        rep = snap.get("reporting", {})

        # 1. Update OPC Card
        opc_bg, opc_fg, opc_txt = self._badge_style_for_state(opc.get("state", "DISCONNECTED"))
        self.opc_badge.configure(bg=opc_bg, fg=opc_fg, text=opc_txt)
        self.opc_server_lbl.configure(text=f"{opc.get('server', '-')} @ {opc.get('host', 'localhost')}")
        self.opc_proto_lbl.configure(text=f"{opc.get('protocol', '-')} (Mode: {opc.get('trigger_mode', '-')})")
        self.opc_poll_lbl.configure(text=f"{opc.get('last_poll_time', '-')} ({opc.get('poll_count', 0)} polls)")
        self.opc_last_trig_lbl.configure(text=f"{opc.get('last_triggered_by', '-')} ({opc.get('last_trigger_time', '-')})")
        self.opc_msg_lbl.configure(text=opc.get("message", ""))

        trig_map = opc.get("trigger_tags") or {}
        bc_val = 0
        bl_val = 0
        for k, v in trig_map.items():
            if "batchcompleted" in k.lower():
                bc_val = 1 if v else 0
            elif "binlevelfull" in k.lower():
                bl_val = 1 if v else 0

        if bc_val == 1:
            self.batch_completed_pill.configure(text="● BatchCompleted: HIGH (1)", bg=c["green_bg"], fg=c["green_fg"])
        else:
            self.batch_completed_pill.configure(text="○ BatchCompleted: LOW (0)", bg=c["gray_bg"], fg=c["gray_fg"])

        if bl_val == 1:
            self.bin_level_full_pill.configure(text="● BinLevelFull: HIGH (1)", bg=c["green_bg"], fg=c["green_fg"])
        else:
            self.bin_level_full_pill.configure(text="○ BinLevelFull: LOW (0)", bg=c["gray_bg"], fg=c["gray_fg"])

        # 2. Update Database Card
        db_bg, db_fg, db_txt = self._badge_style_for_state(db.get("state", "UNKNOWN"))
        self.db_badge.configure(bg=db_bg, fg=db_fg, text=db_txt)
        self.db_engine_lbl.configure(text=f"{db.get('engine', '-')} ({db.get('target', '-')})")
        self.db_total_lbl.configure(text=str(db.get("total_records", 0)))
        self.db_session_lbl.configure(text=f"{db.get('session_captured', 0)} batches")
        self.db_latest_batch_lbl.configure(text=str(db.get("last_batch_no", "-")))
        self.db_latest_time_lbl.configure(text=str(db.get("last_batch_time", "-")))
        self.db_write_status_lbl.configure(text=str(db.get("last_write_status", "Ready")))
        self.db_msg_lbl.configure(text=db.get("message", ""))

        # 3. Update Report Card
        rep_bg, rep_fg, rep_txt = self._badge_style_for_state(rep.get("state", "IDLE"))
        self.rep_badge.configure(bg=rep_bg, fg=rep_fg, text=rep_txt)
        sched_running = rep.get("scheduler_running", False)
        self.rep_sched_lbl.configure(text="ACTIVE (Daily Schedule)" if sched_running else "STANDBY (Manual)")
        self.rep_last_shift_lbl.configure(text=str(rep.get("last_report_shift", "-")))
        last_file = str(rep.get("last_report_file", "-"))
        self.rep_last_file_lbl.configure(text=os.path.basename(last_file) if last_file != "-" else "-")
        self.rep_last_time_lbl.configure(text=f"{rep.get('last_report_time', '-')} ({rep.get('last_report_rows', 0)} rows)")
        self.rep_msg_lbl.configure(text=rep.get("message", ""))

        # 4. Update Overall System Health Banner
        if db.get("state") == "ERROR" or opc.get("state") == "ERROR" or rep.get("state") == "ERROR":
            self.overall_badge.configure(bg=c["red_bg"], fg=c["red_fg"], text="● ATTENTION REQUIRED (CHECK ERRORS)")
        elif opc.get("state") in ("CONNECTED", "SIMULATION") and db.get("state") in ("CONNECTED", "CAPTURING"):
            self.overall_badge.configure(bg=c["green_bg"], fg=c["green_fg"], text="● SYSTEM OPERATIONAL & CAPTURING")
        else:
            self.overall_badge.configure(bg=c["amber_bg"], fg=c["amber_fg"], text="● SYSTEM STANDBY / PARTIAL")

        # 5. Update Recent Captured Database Rows Table
        for item in self.tree.get_children():
            self.tree.delete(item)
        for row in recent_rows:
            formatted = [
                row[0],
                row[1],
                *[f"{float(v):.2f}" if v is not None else "0.00" for v in row[2:]]
            ]
            self.tree.insert("", "end", values=formatted)
        self.table_summary_lbl.configure(text=f"Showing latest {len(recent_rows)} of {db.get('total_records', 0)} records")

        # 6. Append New Log Entries
        new_logs, self._log_cursor = self.ui_log_handler.drain_new(self._log_cursor)
        if new_logs:
            self.log_text.configure(state="normal")
            for level, line in new_logs:
                self.log_text.insert("end", line + "\n", level)
            self.log_text.see("end")
            self.log_text.configure(state="disabled")

    # =========================================================================
    # Built-in Web Dashboard Server (http://localhost:8050)
    # =========================================================================

    def get_web_payload(self):
        rows = self.probe_database_status()
        snap = self.tracker.reload_from_disk()
        with self.ui_log_handler._lock:
            logs = [msg for _, msg in self.ui_log_handler.entries[-40:]]
        formatted_rows = [
            [r[0], r[1], *[f"{float(v):.2f}" if v is not None else "0.00" for v in r[2:]]]
            for r in rows
        ]
        return {
            "status": snap,
            "recent_rows": formatted_rows,
            "logs": logs,
            "server_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        }

    def start_web_server(self, port=8050):
        ui_ref = self

        class DashboardHandler(BaseHTTPRequestHandler):
            def log_message(self, format, *args):
                pass

            def do_GET(self):
                if self.path.startswith("/api/status"):
                    payload = json.dumps(ui_ref.get_web_payload(), default=str).encode("utf-8")
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json; charset=utf-8")
                    self.send_header("Cache-Control", "no-store")
                    self.end_headers()
                    self.wfile.write(payload)
                else:
                    html = WEB_DASHBOARD_HTML.encode("utf-8")
                    self.send_response(200)
                    self.send_header("Content-Type", "text/html; charset=utf-8")
                    self.end_headers()
                    self.wfile.write(html)

            def do_POST(self):
                length = int(self.headers.get("Content-Length", 0) or 0)
                body = self.rfile.read(length).decode("utf-8") if length > 0 else "{}"
                try:
                    req = json.loads(body)
                except Exception:
                    req = {}
                act = req.get("action", "")
                if act == "start_opc_live":
                    ui_ref.start_opc_collector(simulation=False)
                elif act == "start_opc_sim":
                    ui_ref.start_opc_collector(simulation=True)
                elif act == "stop_opc":
                    ui_ref.stop_opc_collector()
                elif act == "pulse_batch_completed":
                    ui_ref.trigger_simulated_rising_edge("BatchCompleted")
                elif act == "pulse_bin_level_full":
                    ui_ref.trigger_simulated_rising_edge("BinLevelFull")
                elif act == "run_shift":
                    ui_ref.trigger_manual_report(req.get("shift", "Shift A"))

                resp = json.dumps({"ok": True, "action": act}).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.end_headers()
                self.wfile.write(resp)

        class ReusableHTTPServer(HTTPServer):
            allow_reuse_address = True

        def _serve():
            for candidate_port in (port, 8051, 8052):
                try:
                    httpd = ReusableHTTPServer(("127.0.0.1", candidate_port), DashboardHandler)
                    logger.info(f"Operator Web Monitoring UI listening at http://localhost:{candidate_port}")
                    httpd.serve_forever()
                    break
                except Exception as e:
                    logger.warning(f"Could not bind Web UI port {candidate_port}: {e}")

        threading.Thread(target=_serve, name="WebDashboardServer", daemon=True).start()


WEB_DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<title>Plant Batching System — Operator Status Monitor</title>
<style>
  :root {
    --bg: #0f172a; --card: #1e293b; --border: #334155; --text: #f8fafc; --muted: #94a3b8;
    --green-bg: #065f46; --green-fg: #34d399; --blue-bg: #1e3a8a; --blue-fg: #60a5fa;
    --amber-bg: #78350f; --amber-fg: #fbbf24; --red-bg: #7f1d1d; --red-fg: #f87171;
  }
  * { box-sizing: border-box; margin: 0; padding: 0; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; }
  body { background: var(--bg); color: var(--text); padding: 18px; }
  .header { background: #020617; border: 1px solid var(--border); border-radius: 10px; padding: 16px 22px; display: flex; justify-content: space-between; align-items: center; margin-bottom: 14px; }
  .header h1 { font-size: 18px; letter-spacing: 0.4px; }
  .header p { font-size: 12px; color: var(--muted); margin-top: 3px; }
  .toolbar { background: var(--card); border: 1px solid var(--border); border-radius: 10px; padding: 10px 16px; display: flex; flex-wrap: wrap; gap: 8px; align-items: center; margin-bottom: 14px; }
  .toolbar span { font-size: 12px; font-weight: 700; color: var(--muted); margin-right: 4px; }
  button { border: none; border-radius: 6px; padding: 7px 12px; font-size: 12px; font-weight: 600; color: #fff; cursor: pointer; transition: opacity 0.15s; }
  button:hover { opacity: 0.88; }
  .btn-green { background: #059669; } .btn-blue { background: #2563eb; } .btn-red { background: #dc2626; } .btn-amber { background: #d97706; }
  .grid { display: grid; grid-template-columns: repeat(3, 1fr); gap: 14px; margin-bottom: 14px; }
  .card { background: var(--card); border: 1px solid var(--border); border-radius: 10px; padding: 16px; }
  .card-hdr { display: flex; justify-content: space-between; align-items: center; margin-bottom: 12px; border-bottom: 1px solid var(--border); padding-bottom: 8px; }
  .card-hdr h2 { font-size: 13px; text-transform: uppercase; letter-spacing: 0.5px; }
  .badge { font-size: 11px; font-weight: 700; padding: 4px 10px; border-radius: 999px; }
  .b-green { background: var(--green-bg); color: var(--green-fg); }
  .b-blue { background: var(--blue-bg); color: var(--blue-fg); }
  .b-amber { background: var(--amber-bg); color: var(--amber-fg); }
  .b-red { background: var(--red-bg); color: var(--red-fg); }
  .b-gray { background: #334155; color: #cbd5e1; }
  .kv { display: flex; justify-content: space-between; font-size: 12.5px; padding: 4px 0; }
  .kv .k { color: var(--muted); } .kv .v { font-weight: 600; text-align: right; }
  .trig-box { background: #0f172a; border: 1px solid var(--border); border-radius: 8px; padding: 10px; margin-top: 10px; }
  .trig-title { font-size: 11px; font-weight: 700; color: var(--muted); margin-bottom: 6px; }
  .pills { display: grid; grid-template-columns: 1fr 1fr; gap: 8px; }
  .pill { text-align: center; padding: 6px 8px; border-radius: 6px; font-size: 11.5px; font-weight: 700; }
  .msg { font-size: 11.5px; color: var(--muted); margin-top: 10px; min-height: 16px; }
  .table-wrap { background: var(--card); border: 1px solid var(--border); border-radius: 10px; padding: 14px 16px; margin-bottom: 14px; overflow-x: auto; }
  table { width: 100%; border-collapse: collapse; font-size: 12px; margin-top: 8px; }
  th, td { padding: 7px 10px; text-align: center; border-bottom: 1px solid var(--border); }
  th { background: #0f172a; color: var(--muted); font-weight: 700; }
  .log-box { background: #020617; border: 1px solid var(--border); border-radius: 10px; padding: 12px 16px; font-family: monospace; font-size: 11.5px; height: 150px; overflow-y: auto; color: #cbd5e1; white-space: pre-wrap; }
</style>
</head>
<body>
  <div class="header">
    <div>
      <h1>PLANT BATCHING SYSTEM — OPERATOR COMMUNICATION & STATUS MONITOR</h1>
      <p>Live OPC Server Communication, PLC BOOL Trigger Tracking (BatchCompleted / BinLevelFull), Database Capture & Reporting Status</p>
    </div>
    <div style="display:flex;align-items:center;gap:14px;">
      <span id="clock" style="font-size:13px;color:var(--muted);"></span>
      <span id="overall-badge" class="badge b-amber">● INITIALIZING</span>
    </div>
  </div>

  <div class="toolbar">
    <span>OPC Controls:</span>
    <button class="btn-green" onclick="postAct('start_opc_live')">Start OPC (Live)</button>
    <button class="btn-blue" onclick="postAct('start_opc_sim')">Start OPC (Sim)</button>
    <button class="btn-red" onclick="postAct('stop_opc')">Stop OPC</button>
    <span style="margin-left:12px;">Test Rising-Edge BOOL Trigger:</span>
    <button class="btn-amber" onclick="postAct('pulse_batch_completed')">Pulse BatchCompleted (LOW→HIGH)</button>
    <button class="btn-amber" onclick="postAct('pulse_bin_level_full')">Pulse BinLevelFull (LOW→HIGH)</button>
  </div>

  <div class="grid">
    <div class="card">
      <div class="card-hdr">
        <h2>1. OPC Server Communication</h2>
        <span id="opc-badge" class="badge b-red">● DISCONNECTED</span>
      </div>
      <div class="kv"><span class="k">Server / Host:</span><span class="v" id="opc-srv">-</span></div>
      <div class="kv"><span class="k">Protocol / Mode:</span><span class="v" id="opc-proto">-</span></div>
      <div class="kv"><span class="k">Last Poll / Cycle Count:</span><span class="v" id="opc-poll">-</span></div>
      <div class="kv"><span class="k">Last Rising-Edge Trigger:</span><span class="v" id="opc-trig">-</span></div>
      <div class="trig-box">
        <div class="trig-title">PLC Rising-Edge BOOL Trigger Tags (LOW → HIGH):</div>
        <div class="pills">
          <div id="pill-bc" class="pill b-gray">○ BatchCompleted: LOW (0)</div>
          <div id="pill-bl" class="pill b-gray">○ BinLevelFull: LOW (0)</div>
        </div>
      </div>
      <div class="msg" id="opc-msg"></div>
    </div>

    <div class="card">
      <div class="card-hdr">
        <h2>2. Database & Data Capturing</h2>
        <span id="db-badge" class="badge b-amber">● CHECKING</span>
      </div>
      <div class="kv"><span class="k">Database Engine / Target:</span><span class="v" id="db-eng">-</span></div>
      <div class="kv"><span class="k">Total Stored Records:</span><span class="v" id="db-tot">0</span></div>
      <div class="kv"><span class="k">Captured This Session:</span><span class="v" id="db-sess">0 batches</span></div>
      <div class="kv"><span class="k">Latest Captured Batch No:</span><span class="v" id="db-bno">-</span></div>
      <div class="kv"><span class="k">Latest Capture Timestamp:</span><span class="v" id="db-btime">-</span></div>
      <div class="kv"><span class="k">Last DB Write Status:</span><span class="v" id="db-wstat">Ready</span></div>
      <div class="msg" id="db-msg"></div>
    </div>

    <div class="card">
      <div class="card-hdr">
        <h2>3. Report Generation Status</h2>
        <span id="rep-badge" class="badge b-green">● READY</span>
      </div>
      <div class="kv"><span class="k">Scheduler Service:</span><span class="v" id="rep-sched">-</span></div>
      <div class="kv"><span class="k">Last Generated Shift:</span><span class="v" id="rep-shift">-</span></div>
      <div class="kv"><span class="k">Last Report File:</span><span class="v" id="rep-file">-</span></div>
      <div class="kv"><span class="k">Generated At / Rows:</span><span class="v" id="rep-time">-</span></div>
      <div style="margin-top:10px;">
        <div class="trig-title">Generate Shift Report Immediately:</div>
        <div style="display:flex;gap:6px;margin-top:4px;">
          <button class="btn-blue" style="flex:1" onclick="runShift('Shift A')">Shift A</button>
          <button class="btn-blue" style="flex:1" onclick="runShift('Shift B')">Shift B</button>
          <button class="btn-blue" style="flex:1" onclick="runShift('Shift C')">Shift C</button>
          <button class="btn-blue" style="flex:1" onclick="runShift('Daily')">Daily</button>
        </div>
      </div>
      <div class="msg" id="rep-msg"></div>
    </div>
  </div>

  <div class="table-wrap">
    <div style="display:flex;justify-content:space-between;align-items:center;">
      <h2 style="font-size:13px;">LATEST CAPTURED BATCH RECORDS IN DATABASE (LIVE VERIFICATION)</h2>
      <span id="tbl-count" style="font-size:12px;color:var(--muted);"></span>
    </div>
    <table>
      <thead>
        <tr>
          <th>BATCH NO.</th><th>BATCHING TIME</th><th>WST BQT H (1)</th><th>QTZ</th>
          <th>WST BQT H (2)</th><th>AN COAL</th><th>WST BQT H (3)</th><th>HARFER COK</th>
          <th>WST BQT L</th><th>WST FBL</th>
        </tr>
      </thead>
      <tbody id="rows-body"></tbody>
    </table>
  </div>

  <div class="log-box" id="log-box">Loading operator logs...</div>

<script>
function badgeClass(state) {
  const s = (state || '').toUpperCase();
  if (['CONNECTED','CAPTURING','SCHEDULED','SUCCESS','READY','HIGH'].includes(s)) return 'badge b-green';
  if (s === 'SIMULATION') return 'badge b-blue';
  if (['CONNECTING','PROCESSING','FALLBACK_SQLITE','IDLE'].includes(s)) return 'badge b-amber';
  if (['ERROR','FAILED','DISCONNECTED'].includes(s)) return 'badge b-red';
  return 'badge b-gray';
}
async function postAct(action, extra={}) {
  await fetch('/api/action', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({action, ...extra})});
  refresh();
}
function runShift(shift) { postAct('run_shift', {shift}); }
async function refresh() {
  try {
    const r = await fetch('/api/status');
    const data = await r.json();
    document.getElementById('clock').textContent = data.server_time;
    const opc = data.status.opc || {}, db = data.status.database || {}, rep = data.status.reporting || {};

    const ob = document.getElementById('opc-badge');
    ob.className = badgeClass(opc.state);
    ob.textContent = '● ' + (opc.state === 'SIMULATION' ? 'CONNECTED (SIMULATION)' : (opc.state || 'DISCONNECTED'));
    document.getElementById('opc-srv').textContent = (opc.server || '-') + ' @ ' + (opc.host || 'localhost');
    document.getElementById('opc-proto').textContent = (opc.protocol || '-') + ' (' + (opc.trigger_mode || '-') + ')';
    document.getElementById('opc-poll').textContent = (opc.last_poll_time || '-') + ' (' + (opc.poll_count || 0) + ' polls)';
    document.getElementById('opc-trig').textContent = (opc.last_triggered_by || '-') + ' (' + (opc.last_trigger_time || '-') + ')';
    document.getElementById('opc-msg').textContent = opc.message || '';

    let bc = 0, bl = 0;
    for (const [k, v] of Object.entries(opc.trigger_tags || {})) {
      if (k.toLowerCase().includes('batchcompleted')) bc = v ? 1 : 0;
      if (k.toLowerCase().includes('binlevelfull')) bl = v ? 1 : 0;
    }
    const pbc = document.getElementById('pill-bc'), pbl = document.getElementById('pill-bl');
    pbc.className = 'pill ' + (bc ? 'b-green' : 'b-gray');
    pbc.textContent = (bc ? '● BatchCompleted: HIGH (1)' : '○ BatchCompleted: LOW (0)');
    pbl.className = 'pill ' + (bl ? 'b-green' : 'b-gray');
    pbl.textContent = (bl ? '● BinLevelFull: HIGH (1)' : '○ BinLevelFull: LOW (0)');

    const dbb = document.getElementById('db-badge');
    dbb.className = badgeClass(db.state);
    dbb.textContent = '● ' + (db.state || 'UNKNOWN');
    document.getElementById('db-eng').textContent = (db.engine || '-') + ' (' + (db.target || '-') + ')';
    document.getElementById('db-tot').textContent = db.total_records || 0;
    document.getElementById('db-sess').textContent = (db.session_captured || 0) + ' batches';
    document.getElementById('db-bno').textContent = db.last_batch_no ?? '-';
    document.getElementById('db-btime').textContent = db.last_batch_time || '-';
    document.getElementById('db-wstat').textContent = db.last_write_status || 'Ready';
    document.getElementById('db-msg').textContent = db.message || '';

    const rb = document.getElementById('rep-badge');
    rb.className = badgeClass(rep.state);
    rb.textContent = '● ' + (rep.state || 'IDLE');
    document.getElementById('rep-sched').textContent = rep.scheduler_running ? 'ACTIVE (Daily Schedule)' : 'STANDBY (Manual)';
    document.getElementById('rep-shift').textContent = rep.last_report_shift || '-';
    const rf = (rep.last_report_file || '-').split(/[\\\\/]/).pop();
    document.getElementById('rep-file').textContent = rf;
    document.getElementById('rep-time').textContent = (rep.last_report_time || '-') + ' (' + (rep.last_report_rows || 0) + ' rows)';
    document.getElementById('rep-msg').textContent = rep.message || '';

    const ov = document.getElementById('overall-badge');
    if ([opc.state, db.state, rep.state].includes('ERROR')) {
      ov.className = 'badge b-red'; ov.textContent = '● ATTENTION REQUIRED';
    } else if (['CONNECTED','SIMULATION'].includes(opc.state) && ['CONNECTED','CAPTURING'].includes(db.state)) {
      ov.className = 'badge b-green'; ov.textContent = '● SYSTEM OPERATIONAL & CAPTURING';
    } else {
      ov.className = 'badge b-amber'; ov.textContent = '● SYSTEM STANDBY';
    }

    const tbody = document.getElementById('rows-body');
    tbody.innerHTML = (data.recent_rows || []).map(row => '<tr>' + row.map(c => '<td>' + c + '</td>').join('') + '</tr>').join('');
    document.getElementById('tbl-count').textContent = 'Showing latest ' + (data.recent_rows || []).length + ' of ' + (db.total_records || 0) + ' records';

    const lb = document.getElementById('log-box');
    lb.textContent = (data.logs || []).join('\\n');
    lb.scrollTop = lb.scrollHeight;
  } catch (e) {}
}
setInterval(refresh, 1500);
refresh();
</script>
</body>
</html>
"""


def launch_operator_ui(config, auto_start_services=True, force_opc_sim=False):
    ui = OperatorMonitorUI(config, auto_start_services=auto_start_services, force_opc_sim=force_opc_sim)
    ui.launch()
