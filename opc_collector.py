import logging
import time
from datetime import datetime
from db_client import DatabaseClient
from opc_client import OPCClient
from monitor_ui import get_status_tracker

logger = logging.getLogger("ReportGenerator.OPCCollector")

class OPCCollector:
    def __init__(self, config, force_simulation=False):
        self.config = config
        self.db_client = DatabaseClient(config)
        self.opc_client = OPCClient(config, force_simulation)
        
        self.opc_config = config.get("opc", {})
        self.poll_interval = self.opc_config.get("polling_interval_seconds", 2)
        self.trigger_mode = self.opc_config.get("trigger_mode", "trigger_tag")
        self.trigger_tag = self.opc_config.get("trigger_tag", "BatchCompleted")
        self.trigger_tags = self._load_trigger_tags()
        
        self.tag_mappings = self.opc_config.get("tag_mappings", {})
        
        # Internal state
        self.last_batch_no = None
        self.last_trigger_val = None
        self.last_trigger_vals = {}  # {tag_name: 0 or 1} for rising-edge (LOW -> HIGH) detection
        self.poll_count = 0
        self.tracker = get_status_tracker()
        self.running = False

    def _load_trigger_tags(self):
        """
        Loads configurable BOOL trigger tags (e.g. BatchCompleted, BinLevelFull) from config.yaml.
        Supports dict (e.g. {'batch_completed': 'BatchCompleted', 'bin_level_full': 'BinLevelFull'}),
        list (e.g. ['BatchCompleted', 'BinLevelFull']), or single legacy trigger_tag string.
        """
        raw = self.opc_config.get("trigger_tags")
        tags = []
        if isinstance(raw, dict):
            for _, tag_name in raw.items():
                if tag_name and str(tag_name).strip():
                    t = str(tag_name).strip()
                    if t not in tags:
                        tags.append(t)
        elif isinstance(raw, list):
            for tag_name in raw:
                if tag_name and str(tag_name).strip():
                    t = str(tag_name).strip()
                    if t not in tags:
                        tags.append(t)
        elif isinstance(raw, str) and raw.strip():
            tags.append(raw.strip())

        if not tags:
            single = self.opc_config.get("trigger_tag")
            if single and str(single).strip():
                tags.append(str(single).strip())
            else:
                tags = ["BatchCompleted", "BinLevelFull"]
        return tags

    @staticmethod
    def _parse_bool_state(val):
        """
        Normalizes a PLC BOOL tag value into 0 (LOW/False) or 1 (HIGH/True).
        Returns None if the tag value is None or unparseable.
        Handles Python bool, int/float (including COM VARIANT_TRUE = -1), and strings.
        """
        if val is None:
            return None
        if isinstance(val, bool):
            return 1 if val else 0
        if isinstance(val, (int, float)):
            return 1 if val != 0 else 0
        if isinstance(val, str):
            s = val.strip().lower()
            if s in ("1", "true", "high", "on", "yes", "-1"):
                return 1
            if s in ("0", "false", "low", "off", "no", ""):
                return 0
            try:
                return 1 if float(s) != 0 else 0
            except ValueError:
                return None
        return None

    def _initialize_db_state(self):
        """
        Attempts to read the latest batch number from the database.
        This prevents inserting duplicate batches if the collector is restarted.
        """
        logger.info("Initializing collector state from database...")
        conn = None
        try:
            # Check SQLite status or SQL Server
            if self.db_client.use_sqlite:
                conn = self.db_client._connect_sqlite()
                table_name = "batching_data"
            else:
                try:
                    conn = self.db_client._connect_sql_server()
                    table_name = "[dbo].[batching_data]"
                except Exception:
                    # Fallback database connection
                    if self.db_client.fallback:
                        self.db_client.use_sqlite = True
                        conn = self.db_client._connect_sqlite()
                        table_name = "batching_data"
                    else:
                        raise

            cursor = conn.cursor()
            cursor.execute(f"SELECT MAX(batch_no) FROM {table_name}")
            row = cursor.fetchone()
            if row and row[0] is not None:
                self.last_batch_no = int(row[0])
                logger.info(f"Last recorded batch number found in database: {self.last_batch_no}")
            else:
                self.last_batch_no = 0
                logger.info("No existing records found in database. Initializing last batch number to 0.")
        except Exception as e:
            logger.warning(f"Could not retrieve max batch_no from database: {e}. Starting fresh.")
            self.last_batch_no = 0
        finally:
            if conn:
                conn.close()

    def start(self):
        """
        Starts the continuous polling and data logging loop.
        """
        self.running = True
        logger.info("Starting OPC DA Data Collector service...")
        
        # Connect to Database & Load state
        self._initialize_db_state()

        # Connect to OPC
        self.tracker.update_opc(
            state="CONNECTING",
            collector_running=True,
            server=self.opc_client.server_name,
            host=self.opc_client.host,
            trigger_mode=self.trigger_mode,
            message=f"Connecting to OPC Server '{self.opc_client.server_name}'...",
        )
        connected = False
        while self.running and not connected:
            try:
                self.opc_client.connect()
                connected = True
                opc_state = "SIMULATION" if self.opc_client.simulation_mode else "CONNECTED"
                self.tracker.update_opc(
                    state=opc_state,
                    collector_running=True,
                    protocol=self.opc_client.active_protocol or self.opc_client.protocol,
                    server=self.opc_client.connected_da_server or self.opc_client.server_name,
                    host=self.opc_client.host,
                    trigger_mode=self.trigger_mode,
                    message=f"Connected via {self.opc_client.active_protocol} to '{self.opc_client.connected_da_server or self.opc_client.server_name}'",
                )
            except Exception as e:
                logger.error(f"Failed to connect to OPC Server: {e}. Retrying in 5 seconds...")
                self.tracker.update_opc(
                    state="ERROR",
                    collector_running=True,
                    message=f"Connection failed: {e}. Retrying in 5s...",
                )
                time.sleep(5)

        logger.info(f"OPC Data Collector started. Mode: {self.trigger_mode}, Poll Interval: {self.poll_interval}s")
        
        # Collect list of all unique tags to read
        tags_to_read = list(self.tag_mappings.values())
        if self.trigger_mode in ("trigger_tag", "trigger_tags", "rising_edge"):
            for t_tag in self.trigger_tags:
                if t_tag not in tags_to_read:
                    tags_to_read.append(t_tag)
            logger.info(f"Monitoring PLC BOOL rising-edge trigger tags (LOW -> HIGH): {self.trigger_tags}")

        # Loop
        try:
            while self.running:
                tick_start = time.time()
                
                try:
                    # Read tags from OPC
                    tag_values = self.opc_client.read_tags(tags_to_read)
                    self.poll_count += 1
                    trig_snapshot = {
                        t: (self._parse_bool_state(tag_values.get(t)) or 0)
                        for t in self.trigger_tags
                    }
                    self.tracker.update_opc(
                        state="SIMULATION" if self.opc_client.simulation_mode else "CONNECTED",
                        collector_running=True,
                        protocol=self.opc_client.active_protocol or self.opc_client.protocol,
                        last_poll_time=datetime.now().strftime("%H:%M:%S"),
                        poll_count=self.poll_count,
                        trigger_tags=trig_snapshot,
                        latest_values=tag_values,
                    )
                    
                    # Process based on trigger mode
                    if self.trigger_mode == "batch_no_change":
                        self._process_batch_no_change(tag_values)
                    elif self.trigger_mode in ("trigger_tag", "trigger_tags", "rising_edge"):
                        self._process_trigger_tag(tag_values)
                    elif self.trigger_mode == "periodic":
                        self._process_periodic(tag_values)
                    else:
                        logger.error(f"Unknown trigger mode: {self.trigger_mode}")
                        
                except Exception as e:
                    logger.error(f"Error during polling cycle: {e}")
                    # Attempt reconnection
                    logger.info("Attempting to reconnect to OPC Server...")
                    try:
                        self.opc_client.disconnect()
                        self.opc_client.connect()
                    except Exception as re_err:
                        logger.error(f"Reconnection failed: {re_err}. Will retry next cycle.")
                
                # Maintain poll interval
                elapsed = time.time() - tick_start
                sleep_time = max(0.1, self.poll_interval - elapsed)
                time.sleep(sleep_time)
                
        except KeyboardInterrupt:
            logger.info("Collector stopped by keyboard interrupt.")
        finally:
            self.stop()

    def stop(self):
        """
        Stops the collector service.
        """
        self.running = False
        logger.info("Stopping OPC DA Data Collector service...")
        self.opc_client.disconnect()
        logger.info("OPC DA Data Collector service stopped.")

    # --- Processing Engines ---
    
    def _write_record(self, tag_values):
        """
        Maps OPC tag values to the database schema and writes to the DB client.
        Auto-increments batch_no if the batch_no tag is unmapped or returns None.
        """
        data_dict = {}
        non_null_count = 0
        # Map OPC tags back to DB fields
        for db_field, tag_name in self.tag_mappings.items():
            val = tag_values.get(tag_name)
            if val is not None:
                non_null_count += 1
            data_dict[db_field] = val

        if non_null_count == 0:
            logger.warning("All configured OPC tags returned None in this cycle; skipping database write.")
            return

        # Auto-assign sequential batch_no if batch_no tag is not mapped in PLC or returned None
        raw_batch_no = data_dict.get("batch_no")
        if raw_batch_no is None:
            self.last_batch_no = (self.last_batch_no or 0) + 1
            data_dict["batch_no"] = self.last_batch_no
        else:
            try:
                data_dict["batch_no"] = int(float(raw_batch_no))
                self.last_batch_no = data_dict["batch_no"]
            except (ValueError, TypeError):
                self.last_batch_no = (self.last_batch_no or 0) + 1
                data_dict["batch_no"] = self.last_batch_no

        now_dt = datetime.now()
        data_dict["batching_time"] = now_dt

        logger.info(f"Writing new record to database. Batch No: {data_dict.get('batch_no')}")
        self.db_client.insert_batching_data(data_dict)
        self.tracker.record_db_capture(
            batch_no=data_dict.get("batch_no"),
            batch_time_str=now_dt.strftime("%Y-%m-%d %H:%M:%S"),
            engine_label="SQLite" if self.db_client.use_sqlite else "SQL Server",
        )

    def _process_batch_no_change(self, tag_values):
        """
        Triggers database write when the batch number increments.
        """
        batch_no_tag = self.tag_mappings.get("batch_no")
        if not batch_no_tag:
            logger.error("No mapping defined for 'batch_no' tag. Cannot use batch_no_change mode.")
            return
            
        current_batch_no = tag_values.get(batch_no_tag)
        if current_batch_no is None:
            logger.warning("Batch number tag read as None. Skipping cycle.")
            return

        try:
            current_batch_no = int(current_batch_no)
        except ValueError:
            logger.error(f"Batch number '{current_batch_no}' is not a valid integer. Skipping.")
            return

        # Initialize if not set (edge case on first read if DB was empty)
        if self.last_batch_no is None:
            self.last_batch_no = current_batch_no
            logger.info(f"Initialized last batch number to: {self.last_batch_no}")
            return

        # Trigger if current batch is greater than last recorded
        if current_batch_no > self.last_batch_no:
            logger.info(f"Batch number change detected: {self.last_batch_no} -> {current_batch_no}")
            self._write_record(tag_values)
            self.last_batch_no = current_batch_no
        elif current_batch_no < self.last_batch_no:
            # Handle PLC rollover or reset
            logger.warning(f"Batch number decreased: {self.last_batch_no} -> {current_batch_no}. Resetting tracked batch number.")
            self.last_batch_no = current_batch_no

    def _process_trigger_tag(self, tag_values):
        """
        Triggers database write whenever ANY configured BOOL trigger tag
        (e.g. BatchCompleted or BinLevelFull) transitions from LOW (0/False) to HIGH (1/True).
        """
        active_triggers = self.trigger_tags if getattr(self, "trigger_tags", None) else [self.trigger_tag]
        rising_edge_tags = []

        for t_tag in active_triggers:
            if not t_tag:
                continue
            current_state = self._parse_bool_state(tag_values.get(t_tag))
            if current_state is None:
                continue

            prev_state = self.last_trigger_vals.get(t_tag)
            if prev_state is None:
                # First observation: initialize baseline state
                self.last_trigger_vals[t_tag] = current_state
                self.last_trigger_val = current_state
                logger.info(f"Initialized BOOL trigger tag '{t_tag}' state to {'HIGH (1)' if current_state == 1 else 'LOW (0)'}")
                continue

            # Detect rising edge transition: LOW (0) -> HIGH (1)
            if prev_state == 0 and current_state == 1:
                rising_edge_tags.append(t_tag)

            self.last_trigger_vals[t_tag] = current_state
            self.last_trigger_val = current_state

        if rising_edge_tags:
            logger.info(f"Rising-edge trigger (LOW -> HIGH) detected on PLC BOOL tag(s): {rising_edge_tags}")
            self.tracker.update_opc(
                last_triggered_by=", ".join(rising_edge_tags),
                last_trigger_time=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                message=f"Rising-edge trigger (LOW -> HIGH) on {', '.join(rising_edge_tags)}",
            )
            self._write_record(tag_values)

    def _process_periodic(self, tag_values):
        """
        Periodically writes the current values to the database.
        """
        self._write_record(tag_values)
