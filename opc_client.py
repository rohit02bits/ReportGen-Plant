import logging
import time
import random
from datetime import datetime

logger = logging.getLogger("ReportGenerator.OPCClient")

class OPCClient:
    def __init__(self, config, force_simulation=False):
        self.config = config
        self.opc_config = config.get("opc", {})
        self.server_name = self.opc_config.get("server", "ABB.DriveOPC")
        self.host = self.opc_config.get("host", "localhost")
        self.simulation_enabled = self.opc_config.get("simulation", {}).get("enabled", True)
        
        # Determine if we should use simulation mode
        self.simulation_mode = force_simulation or not self._is_windows_platform()
        if not self.simulation_mode and not self.simulation_enabled:
            self.simulation_mode = False
        elif not self.simulation_mode and self.simulation_enabled:
            # If user explicitly enables simulation in config, respect it
            self.simulation_mode = True

        self.client = None
        self.com_server = None
        self.connected = False
        
        # Simulation State
        if self.simulation_mode:
            logger.info("OPC Client initialized in SIMULATION mode.")
            self.sim_batch_no = random.randint(100, 500)
            self.sim_interval = self.opc_config.get("simulation", {}).get("interval_seconds", 15)
            self.sim_last_update = time.time()
            self.sim_tags_cache = self._generate_simulated_tags()
        else:
            logger.info(f"OPC Client initialized in REAL mode targeting server '{self.server_name}' on host '{self.host}'")

    def _is_windows_platform(self):
        import sys
        return sys.platform == "win32"

    def connect(self):
        """
        Connects to the OPC DA server (or initializes simulation).
        """
        if self.simulation_mode:
            logger.info("Connecting to Simulated ABB OPC Server...")
            time.sleep(0.5)  # Simulate network latency
            self.connected = True
            logger.info("Connected to Simulated ABB OPC Server successfully.")
            return True

        # Real connection attempt (Windows only)
        logger.info(f"Attempting to connect to real OPC Server '{self.server_name}'...")
        
        # Try OpenOPC first
        try:
            import OpenOPC
            logger.info("Imported OpenOPC successfully. Attempting connection...")
            self.client = OpenOPC.client()
            # If host is localhost, OpenOPC connects locally
            if self.host and self.host != "localhost":
                self.client.connect(self.server_name, self.host)
            else:
                self.client.connect(self.server_name)
            self.connected = True
            logger.info(f"Successfully connected to OPC Server '{self.server_name}' using OpenOPC.")
            return True
        except ImportError:
            logger.warning("OpenOPC library is not installed. Trying win32com.client automation fallback...")
        except Exception as e:
            logger.warning(f"OpenOPC connection failed: {e}. Trying win32com.client fallback...")

        # Fallback to win32com.client directly
        try:
            import win32com.client
            logger.info("Imported win32com.client. Dispatches OPC.Automation...")
            self.com_server = win32com.client.Dispatch("OPC.Automation")
            self.com_server.Connect(self.server_name, self.host)
            self.connected = True
            logger.info(f"Successfully connected to OPC Server '{self.server_name}' using win32com.client.")
            return True
        except ImportError:
            logger.error("win32com.client is not available (non-Windows platform or not installed).")
        except Exception as e:
            logger.error(f"win32com direct COM connection failed: {e}")

        # If both fail, check if we can fall back to simulation
        if self.simulation_enabled:
            logger.warning("All live OPC DA connections failed. Falling back to Simulation Mode as enabled in config.")
            self.simulation_mode = True
            self.sim_batch_no = random.randint(100, 500)
            self.sim_interval = self.opc_config.get("simulation", {}).get("interval_seconds", 15)
            self.sim_last_update = time.time()
            self.sim_tags_cache = self._generate_simulated_tags()
            self.connected = True
            return True
        
        self.connected = False
        raise ConnectionError(f"Could not connect to OPC DA Server '{self.server_name}'")

    def disconnect(self):
        """
        Disconnects from the OPC server.
        """
        if self.simulation_mode:
            logger.info("Disconnected from Simulated OPC Server.")
            self.connected = False
            return

        if self.client:
            try:
                self.client.close()
                logger.info("OpenOPC connection closed.")
            except Exception as e:
                logger.error(f"Error closing OpenOPC client: {e}")
            finally:
                self.client = None

        if self.com_server:
            try:
                self.com_server.Disconnect()
                logger.info("win32com OPC connection closed.")
            except Exception as e:
                logger.error(f"Error disconnecting win32com client: {e}")
            finally:
                self.com_server = None
        
        self.connected = False

    def read_tags(self, tags):
        """
        Reads values for a list of tags. Returns a dict mapping tag names to values.
        """
        if not self.connected:
            raise RuntimeError("OPC Client is not connected. Call connect() first.")

        if self.simulation_mode:
            self._update_simulation()
            # Return requested tag values from cache
            result = {}
            for t in tags:
                result[t] = self.sim_tags_cache.get(t, 0.0)
            return result

        # Read using OpenOPC
        if self.client:
            try:
                # OpenOPC read can accept a list of tags. Returns list of (tag, value, quality, time)
                reads = self.client.read(tags)
                if not isinstance(reads, list):
                    reads = [reads]
                
                result = {}
                for item in reads:
                    if isinstance(item, tuple) and len(item) >= 2:
                        tag_name, val = item[0], item[1]
                        result[tag_name] = val
                
                # Check for missing tags and set them to None
                for t in tags:
                    if t not in result:
                        logger.warning(f"OPC read did not return tag '{t}'")
                        result[t] = None
                return result
            except Exception as e:
                logger.error(f"OpenOPC read error: {e}")
                raise e

        # Read using win32com OPC.Automation
        if self.com_server:
            try:
                temp_group = self.com_server.OPCGroups.Add("TempReadGroup")
                temp_group.IsActive = True
                
                opc_items = temp_group.OPCItems
                item_count = len(tags)
                
                server_handles = []
                result = {}
                for idx, t in enumerate(tags):
                    item = opc_items.AddItem(t, idx + 1)
                    server_handles.append(item.ServerHandle)
                
                handles_arr = [0] + server_handles
                values, errors, qualities, timestamps = temp_group.SyncRead(2, item_count, handles_arr)
                
                for idx, t in enumerate(tags):
                    result[t] = values[idx + 1]
                
                self.com_server.OPCGroups.Remove("TempReadGroup")
                return result
            except Exception as e:
                logger.error(f"win32com read error: {e}")
                raise e

        raise RuntimeError("No active OPC driver connection available.")

    # --- Simulation Engine Methods ---
    def _update_simulation(self):
        """
        Simulates progress of a physical batching process.
        """
        now = time.time()
        elapsed = now - self.sim_last_update
        
        # If the elapsed time exceeds the simulated batch interval, we run a new batch
        if elapsed >= self.sim_interval:
            self.sim_batch_no += 1
            self.sim_last_update = now
            self.sim_tags_cache = self._generate_simulated_tags()
            logger.info(f"[OPC Simulation] New batch generated! Batch No: {self.sim_batch_no}")

    def _generate_simulated_tags(self):
        """
        Generates realistic random values for simulated OPC tags.
        """
        mappings = self.opc_config.get("tag_mappings", {})
        trigger_tag = self.opc_config.get("trigger_tag", "ABB_Drive.BatchComplete")
        
        # Realistic ranges
        sim_vals = {
            mappings.get("batch_no", "ABB_Drive.BatchNo"): self.sim_batch_no,
            mappings.get("empty_val_1", "ABB_Drive.EmptyVal1"): 0.00,
            mappings.get("wst_bqt_h_1", "ABB_Drive.WstBqtH1"): round(random.uniform(695.0, 706.0), 2),
            mappings.get("qtz", "ABB_Drive.Qtz"): round(random.uniform(240.0, 280.0), 2),
            mappings.get("wst_bqt_h_2", "ABB_Drive.WstBqtH2"): 0.00,
            mappings.get("empty_val_2", "ABB_Drive.EmptyVal2"): 0.00,
            mappings.get("an_coal", "ABB_Drive.AnCoal"): 0.00,
            mappings.get("wst_bqt_h_3", "ABB_Drive.WstBqtH3"): round(random.uniform(1390.0, 1415.0), 2),
            mappings.get("harfer_cok", "ABB_Drive.HarferCok"): round(random.choice([
                random.uniform(280.0, 295.0),
                random.uniform(280.0, 295.0),
                random.uniform(130.0, 150.0),
                random.uniform(400.0, 450.0)
            ]), 2),
            mappings.get("wst_bqt_l", "ABB_Drive.WstBqtL"): round(random.uniform(645.0, 660.0), 2),
            mappings.get("wst_fbl", "ABB_Drive.WstFbl"): round(random.uniform(245.0, 260.0), 2),
            trigger_tag: 0
        }
        
        # Trigger tag simulation: for the last 3 seconds of a batch, set trigger to 1/True
        now = time.time()
        elapsed = now - getattr(self, 'sim_last_update', now)
        sim_interval = getattr(self, 'sim_interval', 15)
        if sim_interval - elapsed <= 3:
            sim_vals[trigger_tag] = 1
            
        return sim_vals
