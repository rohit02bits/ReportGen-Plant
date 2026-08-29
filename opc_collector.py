import logging
import time
from datetime import datetime
from db_client import DatabaseClient
from opc_client import OPCClient

logger = logging.getLogger("ReportGenerator.OPCCollector")

class OPCCollector:
    def __init__(self, config, force_simulation=False):
        self.config = config
        self.db_client = DatabaseClient(config)
        self.opc_client = OPCClient(config, force_simulation)
        
        self.opc_config = config.get("opc", {})
        self.poll_interval = self.opc_config.get("polling_interval_seconds", 2)
        self.trigger_mode = self.opc_config.get("trigger_mode", "batch_no_change")
        self.trigger_tag = self.opc_config.get("trigger_tag", "ABB_Drive.BatchComplete")
        
        self.tag_mappings = self.opc_config.get("tag_mappings", {})
        
        # Internal state
        self.last_batch_no = None
        self.last_trigger_val = None
        self.running = False

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
        connected = False
        while self.running and not connected:
            try:
                self.opc_client.connect()
                connected = True
            except Exception as e:
                logger.error(f"Failed to connect to OPC Server: {e}. Retrying in 5 seconds...")
                time.sleep(5)

        logger.info(f"OPC Data Collector started. Mode: {self.trigger_mode}, Poll Interval: {self.poll_interval}s")
        
        # Collect list of all unique tags to read
        tags_to_read = list(self.tag_mappings.values())
        if self.trigger_mode == "trigger_tag" and self.trigger_tag not in tags_to_read:
            tags_to_read.append(self.trigger_tag)

        # Loop
        try:
            while self.running:
                tick_start = time.time()
                
                try:
                    # Read tags from OPC
                    tag_values = self.opc_client.read_tags(tags_to_read)
                    
                    # Process based on trigger mode
                    if self.trigger_mode == "batch_no_change":
                        self._process_batch_no_change(tag_values)
                    elif self.trigger_mode == "trigger_tag":
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
        """
        data_dict = {}
        # Map OPC tags back to DB fields
        for db_field, tag_name in self.tag_mappings.items():
            data_dict[db_field] = tag_values.get(tag_name)
            
        data_dict["batching_time"] = datetime.now()
        
        logger.info(f"Writing new record to database. Batch No: {data_dict.get('batch_no')}")
        self.db_client.insert_batching_data(data_dict)

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
        Triggers database write on rising edge of trigger tag (0 -> 1).
        """
        current_trigger = tag_values.get(self.trigger_tag)
        if current_trigger is None:
            return

        try:
            current_trigger = int(current_trigger)
        except ValueError:
            return

        if self.last_trigger_val is None:
            self.last_trigger_val = current_trigger
            return

        # Detect transition from 0 to 1 (or False to True)
        if self.last_trigger_val == 0 and current_trigger == 1:
            logger.info(f"OPC Trigger event detected on tag '{self.trigger_tag}'")
            self._write_record(tag_values)
            
        self.last_trigger_val = current_trigger

    def _process_periodic(self, tag_values):
        """
        Periodically writes the current values to the database.
        """
        self._write_record(tag_values)
