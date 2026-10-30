import os
import sys
import time
import random
import logging
import asyncio
from datetime import datetime

logger = logging.getLogger("ReportGenerator.OPCClient")

class OPCClient:
    """
    Dual-protocol OPC Client supporting both:
    1. Modern OPC UA (Cross-platform via asyncua / opcua, tcp socket opc.tcp://...)
    2. Classic OPC DA (Windows COM/DCOM via OpenOPC or win32com, e.g. ABB.DriveOPC)
    3. Built-in Simulation Engine for offline testing and non-Windows environments.
    """
    def __init__(self, config, force_simulation=False):
        self.config = config
        self.opc_config = config.get("opc", {})
        
        # Protocol: 'auto', 'opc_ua', or 'opc_da'
        self.protocol = str(self.opc_config.get("protocol", "auto")).strip().lower()
        
        # OPC UA settings
        self.endpoint_url = self.opc_config.get("endpoint_url", "opc.tcp://localhost:4840")
        self.ua_username = self.opc_config.get("username", "")
        self.ua_password = self.opc_config.get("password", "")
        
        # OPC DA settings (ABB DriveOPC)
        self.server_name = self.opc_config.get("server", "ABB.DriveOPC")
        self.host = self.opc_config.get("host", "localhost")
        
        # Simulation settings
        self.simulation_enabled = self.opc_config.get("simulation", {}).get("enabled", True)
        
        # Active driver references
        self.active_protocol = None  # 'opc_ua', 'opc_ua_sync', 'opc_da_openopc', 'opc_da_win32', or 'simulation'
        self.ua_client = None
        self.da_client = None
        self.da_com_server = None
        self.connected = False
        
        # Determine simulation mode
        # If forced, or if configured to DA on non-Windows, or if explicit simulation is requested
        if force_simulation:
            self.simulation_mode = True
        elif not self._is_windows_platform() and self.protocol in ["opc_da", "da"]:
            logger.info("Non-Windows OS detected with OPC DA requested. Routing to Simulation mode.")
            self.simulation_mode = True
        else:
            self.simulation_mode = False
            
        # Simulation State Initialization
        self.sim_batch_no = random.randint(100, 500)
        self.sim_interval = self.opc_config.get("simulation", {}).get("interval_seconds", 15)
        self.sim_last_update = time.time()
        self.sim_tags_cache = self._generate_simulated_tags()
        
        if self.simulation_mode:
            logger.info("OPC Client initialized in SIMULATION mode.")
        else:
            logger.info(f"OPC Client initialized (Protocol: {self.protocol}, DA Server: '{self.server_name}', UA Endpoint: '{self.endpoint_url}')")

    def _is_windows_platform(self):
        return sys.platform == "win32"

    def _run_async(self, coro):
        """Runs an async coroutine synchronously, handling any active event loops."""
        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                import concurrent.futures
                with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                    return pool.submit(asyncio.run, coro).result()
            return loop.run_until_complete(coro)
        except RuntimeError:
            return asyncio.run(coro)

    def _fallback_simulation(self, reason):
        """Switches client to simulation mode if simulation is enabled."""
        if self.simulation_enabled:
            logger.warning(f"{reason}. Falling back to Simulation Mode as enabled in configuration.")
            self.simulation_mode = True
            self.active_protocol = "simulation"
            self.connected = True
            return True
        self.connected = False
        raise ConnectionError(f"OPC Connection failed and simulation fallback is disabled: {reason}")

    # =========================================================================
    # Connection Management
    # =========================================================================

    def connect(self):
        """
        Connects to the OPC server using the configured or auto-detected protocol.
        """
        if self.simulation_mode:
            logger.info("Connecting to Simulated ABB OPC Server...")
            time.sleep(0.3)
            self.active_protocol = "simulation"
            self.connected = True
            logger.info("Connected to Simulated ABB OPC Server successfully.")
            return True

        # 1. OPC UA Protocol Path
        if self.protocol in ["opc_ua", "ua"] or (self.protocol == "auto" and str(self.endpoint_url).startswith("opc.tcp://")):
            try:
                connected = self._connect_opc_ua()
                if connected:
                    return True
            except Exception as e:
                logger.warning(f"OPC UA connection attempt failed: {e}")
                if self.protocol in ["opc_ua", "ua"]:
                    return self._fallback_simulation(f"OPC UA connection failed: {e}")

        # 2. OPC DA Protocol Path (Windows COM - e.g. ABB.DriveOPC)
        if self.protocol in ["opc_da", "da", "auto"] and self._is_windows_platform():
            try:
                connected = self._connect_opc_da()
                if connected:
                    return True
            except Exception as e:
                logger.warning(f"OPC DA connection attempt failed: {e}")
                if self.protocol in ["opc_da", "da"]:
                    return self._fallback_simulation(f"OPC DA connection failed: {e}")

        # 3. If neither succeeded, fallback to simulation
        return self._fallback_simulation("All live OPC (UA and DA) connection attempts failed")

    def _connect_opc_ua(self):
        """Attempts connection to an OPC UA server."""
        logger.info(f"Attempting connection to OPC UA Server: {self.endpoint_url}")
        
        # Try modern asyncua first
        try:
            from asyncua import Client
            self.ua_client = Client(url=self.endpoint_url)
            if self.ua_username and self.ua_password:
                self.ua_client.set_user(self.ua_username)
                self.ua_client.set_password(self.ua_password)
                
            async def _do_ua_connect():
                await self.ua_client.connect()
                
            self._run_async(_do_ua_connect())
            self.active_protocol = "opc_ua"
            self.connected = True
            logger.info(f"Successfully connected to OPC UA Server at {self.endpoint_url}")
            return True
        except ImportError:
            logger.debug("asyncua library not installed. Checking for legacy opcua library...")
        except Exception as e:
            logger.warning(f"asyncua connection error: {e}")

        # Try legacy opcua fallback
        try:
            import opcua
            self.ua_client = opcua.Client(self.endpoint_url)
            if self.ua_username and self.ua_password:
                self.ua_client.set_user(self.ua_username)
                self.ua_client.set_password(self.ua_password)
            self.ua_client.connect()
            self.active_protocol = "opc_ua_sync"
            self.connected = True
            logger.info(f"Successfully connected to OPC UA Server (via opcua) at {self.endpoint_url}")
            return True
        except ImportError:
            logger.warning("Neither 'asyncua' nor 'opcua' library is installed on this system.")
        except Exception as e:
            logger.warning(f"opcua connection error: {e}")

        raise ConnectionError(f"Could not connect to OPC UA Server '{self.endpoint_url}'")

    def _connect_opc_da(self):
        """Attempts connection to an OPC DA server (e.g. ABB.DriveOPC)."""
        logger.info(f"Attempting connection to OPC DA Server '{self.server_name}' on host '{self.host}'...")
        
        # Candidate ProgIDs for ABB DriveOPC
        candidates = [self.server_name]
        if "abb" in self.server_name.lower():
            for alt in ["ABB.DriveOPC", "ABB.DriveDA", "ABB.DriveOPC.1", "ABB.DriveDA.1"]:
                if alt not in candidates:
                    candidates.append(alt)

        # 1. Try OpenOPC
        try:
            import OpenOPC
            for srv in candidates:
                try:
                    logger.debug(f"Trying OpenOPC with server ProgID '{srv}'...")
                    client = OpenOPC.client()
                    if self.host and self.host != "localhost":
                        client.connect(srv, self.host)
                    else:
                        client.connect(srv)
                    self.da_client = client
                    self.active_protocol = "opc_da_openopc"
                    self.connected = True
                    logger.info(f"Successfully connected to OPC DA Server '{srv}' using OpenOPC.")
                    return True
                except Exception as ex:
                    logger.debug(f"OpenOPC failed for '{srv}': {ex}")
        except ImportError:
            logger.warning("OpenOPC library is not installed. Trying win32com.client fallback...")

        # 2. Try win32com.client OPC.Automation
        try:
            import win32com.client
            for srv in candidates:
                try:
                    logger.debug(f"Trying win32com OPC.Automation with server ProgID '{srv}'...")
                    com_srv = win32com.client.Dispatch("OPC.Automation")
                    com_srv.Connect(srv, self.host)
                    self.da_com_server = com_srv
                    self.active_protocol = "opc_da_win32"
                    self.connected = True
                    logger.info(f"Successfully connected to OPC DA Server '{srv}' using win32com.client.")
                    return True
                except Exception as ex:
                    logger.debug(f"win32com failed for '{srv}': {ex}")
        except ImportError:
            logger.warning("win32com.client is not available on this platform.")

        raise ConnectionError(f"Could not connect to any OPC DA candidate servers: {candidates}")

    def disconnect(self):
        """Disconnects from the active OPC server."""
        if self.simulation_mode:
            logger.info("Disconnected from Simulated OPC Server.")
            self.connected = False
            self.active_protocol = None
            return

        # Disconnect OPC UA
        if self.active_protocol == "opc_ua" and self.ua_client:
            try:
                async def _do_disconnect():
                    await self.ua_client.disconnect()
                self._run_async(_do_disconnect())
                logger.info("OPC UA connection closed.")
            except Exception as e:
                logger.error(f"Error disconnecting OPC UA client: {e}")
            finally:
                self.ua_client = None

        elif self.active_protocol == "opc_ua_sync" and self.ua_client:
            try:
                self.ua_client.disconnect()
                logger.info("OPC UA connection closed.")
            except Exception as e:
                logger.error(f"Error disconnecting legacy opcua client: {e}")
            finally:
                self.ua_client = None

        # Disconnect OpenOPC
        if self.da_client:
            try:
                self.da_client.close()
                logger.info("OpenOPC connection closed.")
            except Exception as e:
                logger.error(f"Error closing OpenOPC client: {e}")
            finally:
                self.da_client = None

        # Disconnect win32com
        if self.da_com_server:
            try:
                self.da_com_server.Disconnect()
                logger.info("win32com OPC connection closed.")
            except Exception as e:
                logger.error(f"Error disconnecting win32com client: {e}")
            finally:
                self.da_com_server = None

        self.connected = False
        self.active_protocol = None

    # =========================================================================
    # Tag Reading
    # =========================================================================

    def read_tags(self, tags):
        """
        Reads values for a list of tags. Returns a dict mapping tag names to values.
        Supports both OPC UA NodeIds and OPC DA tag names.
        """
        if not self.connected:
            raise RuntimeError("OPC Client is not connected. Call connect() first.")

        if self.simulation_mode:
            self._update_simulation()
            result = {}
            for t in tags:
                result[t] = self.sim_tags_cache.get(t, 0.0)
            return result

        # OPC UA Read
        if self.active_protocol == "opc_ua":
            return self._read_tags_opc_ua_async(tags)
        elif self.active_protocol == "opc_ua_sync":
            return self._read_tags_opc_ua_sync(tags)

        # OPC DA Read via OpenOPC
        if self.active_protocol == "opc_da_openopc" and self.da_client:
            return self._read_tags_openopc(tags)

        # OPC DA Read via win32com
        if self.active_protocol == "opc_da_win32" and self.da_com_server:
            return self._read_tags_win32com(tags)

        raise RuntimeError(f"No active OPC driver connection available (Protocol: {self.active_protocol})")

    def _read_tags_opc_ua_async(self, tags):
        async def _do_read():
            results = {}
            for tag in tags:
                try:
                    node = self.ua_client.get_node(tag)
                    val = await node.read_value()
                    results[tag] = val
                except Exception as err:
                    logger.warning(f"Error reading OPC UA node '{tag}': {err}")
                    results[tag] = None
            return results
        return self._run_async(_do_read())

    def _read_tags_opc_ua_sync(self, tags):
        results = {}
        for tag in tags:
            try:
                node = self.ua_client.get_node(tag)
                results[tag] = node.get_value()
            except Exception as err:
                logger.warning(f"Error reading OPC UA node '{tag}': {err}")
                results[tag] = None
        return results

    def _read_tags_openopc(self, tags):
        try:
            reads = self.da_client.read(tags)
            if not isinstance(reads, list):
                reads = [reads]
            
            result = {}
            for item in reads:
                if isinstance(item, tuple) and len(item) >= 2:
                    tag_name, val = item[0], item[1]
                    result[tag_name] = val

            for t in tags:
                if t not in result:
                    logger.warning(f"OpenOPC read did not return tag '{t}'")
                    result[t] = None
            return result
        except Exception as e:
            logger.error(f"OpenOPC read error: {e}")
            raise e

    def _read_tags_win32com(self, tags):
        try:
            temp_group = self.da_com_server.OPCGroups.Add("TempReadGroup")
            temp_group.IsActive = True
            
            opc_items = temp_group.OPCItems
            item_count = len(tags)
            
            server_handles = []
            for idx, t in enumerate(tags):
                item = opc_items.AddItem(t, idx + 1)
                server_handles.append(item.ServerHandle)
            
            handles_arr = [0] + server_handles
            values, errors, qualities, timestamps = temp_group.SyncRead(2, item_count, handles_arr)
            
            result = {}
            for idx, t in enumerate(tags):
                result[t] = values[idx + 1]
            
            self.da_com_server.OPCGroups.Remove("TempReadGroup")
            return result
        except Exception as e:
            logger.error(f"win32com read error: {e}")
            raise e

    # =========================================================================
    # Simulation Engine Methods
    # =========================================================================

    def _update_simulation(self):
        """Simulates progress of a physical batching process."""
        now = time.time()
        elapsed = now - self.sim_last_update
        
        if elapsed >= self.sim_interval:
            self.sim_batch_no += 1
            self.sim_last_update = now
            self.sim_tags_cache = self._generate_simulated_tags()
            logger.info(f"[OPC Simulation] New batch generated! Batch No: {self.sim_batch_no}")

    def _generate_simulated_tags(self):
        """Generates realistic random values for simulated ABB drive tags."""
        mappings = self.opc_config.get("tag_mappings", {})
        trigger_tag = self.opc_config.get("trigger_tag", "ABB_Drive.BatchComplete")
        
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
        
        # Trigger tag simulation: for the last 3 seconds of a batch, set trigger to 1
        now = time.time()
        elapsed = now - getattr(self, 'sim_last_update', now)
        sim_interval = getattr(self, 'sim_interval', 15)
        if sim_interval - elapsed <= 3:
            sim_vals[trigger_tag] = 1
            
        return sim_vals
