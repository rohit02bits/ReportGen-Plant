import os
import sys
import time
import json
import socket
import random
import logging
import asyncio
import subprocess
from datetime import datetime

logger = logging.getLogger("ReportGenerator.OPCClient")


def _clean_str(val, default=""):
    r"""Strips whitespace and repairs YAML double-quote backslash escapes (\P -> U+2029, \L -> U+2028, \N -> U+0085)."""
    if val is None:
        return default
    s = (
        str(val)
        .strip()
        .replace("\u2029", "\\P")
        .replace("\u2028", "\\L")
        .replace("\u0085", "\\N")
    )
    for ch in ("\ufeff", "\u200b", "\r", "\n", "\t"):
        s = s.replace(ch, "")
    return s.strip() or default


class OPCClient:
    """
    Dual-protocol OPC Client supporting both:
    1. Classic OPC DA (Windows COM/DCOM via OpenOPC, win32com AddItems/SyncRead, or 32-bit WOW64 COM Bridge, e.g. ABB.AC800MC_OpcDaServer.3)
    2. Modern OPC UA (Cross-platform via asyncua / opcua, tcp socket opc.tcp://...)
    3. Built-in Simulation Engine for offline testing and non-Windows environments.
    """

    # Known OPC DA Automation Wrapper ProgIDs across vendors (OPC Foundation, Graybox, Matrikon, ABB, Rockwell, etc.)
    DA_WRAPPER_PROGIDS = [
        "OPC.Automation",
        "OPC.Automation.1",
        "Graybox.OPC.DAWrapper",
        "Graybox.OPC.DAWrapper.1",
        "Matrikon.OPC.Automation",
        "Matrikon.OPC.Automation.1",
        "ABB.OPC.Automation",
        "ABB.OPC.Automation.1",
        "OPCAutomation.OPCServer",
        "RSI.OPCAutomation",
        "HSCOPC.Automation",
    ]

    # Standard OPC Foundation OPCDAAuto.dll GUIDs
    OPC_DA_AUTO_CLSID = "{28E68F9A-8D75-11D1-8DC3-3C302A000000}"
    OPC_DA_AUTO_LIBID = "{28E68F91-8D75-11D1-8DC3-3C302A000000}"
    PSOA_INTERFACE_CLSID = "{00020424-0000-0000-C000-000000000046}"

    # InvokeTypes signatures for OPCDAAuto.dll OPCItems.AddItems and OPCGroup.SyncRead
    # Avoids calling OPCItems.AddItem which returns COPCItem (missing COM_INTERFACE_ENTRY(IDispatch) in OPCDAAuto.dll, causing E_NOINTERFACE -2147467262)
    # 3 = VT_I4, 2 = VT_I2, 8 = VT_BSTR, 12 = VT_VARIANT
    # 24584 = VT_ARRAY | VT_BYREF | VT_BSTR
    # 24579 = VT_ARRAY | VT_BYREF | VT_I4
    # 24588 = VT_ARRAY | VT_BYREF | VT_VARIANT
    # 16396 = VT_BYREF | VT_VARIANT
    ADD_ITEMS_ARG_TYPES = (
        (3, 1),       # [in] long NumItems
        (24584, 1),   # [in] SAFEARRAY(BSTR)* ItemIDs
        (24579, 1),   # [in] SAFEARRAY(long)* ClientHandles
        (24579, 2),   # [out] SAFEARRAY(long)* ServerHandles
        (24579, 2),   # [out] SAFEARRAY(long)* Errors
        (12, 17),     # [in, optional] VARIANT RequestedDataTypes
        (12, 17),     # [in, optional] VARIANT AccessPaths
    )

    SYNC_READ_ARG_TYPES = (
        (2, 1),       # [in] short Source (1 = OPCCache, 2 = OPCDevice)
        (3, 1),       # [in] long NumItems
        (24579, 1),   # [in] SAFEARRAY(long)* ServerHandles
        (24588, 2),   # [out] SAFEARRAY(VARIANT)* Values
        (24579, 2),   # [out] SAFEARRAY(long)* Errors
        (16396, 18),  # [out, optional] VARIANT* Qualities
        (16396, 18),  # [out, optional] VARIANT* TimeStamps
    )

    def __init__(self, config, force_simulation=False):
        self.config = config
        self.opc_config = config.get("opc", {})

        # Protocol: 'auto', 'opc_ua', or 'opc_da'
        self.protocol = _clean_str(self.opc_config.get("protocol", "auto"), "auto").lower()

        # OPC UA settings
        self.endpoint_url = _clean_str(self.opc_config.get("endpoint_url", "opc.tcp://localhost:4840"), "opc.tcp://localhost:4840")
        self.ua_username = _clean_str(self.opc_config.get("username", ""))
        self.ua_password = _clean_str(self.opc_config.get("password", ""))

        # OPC DA settings (ABB AC800M / ABB DriveOPC)
        self.server_name = _clean_str(self.opc_config.get("server", "ABB.AC800MC_OpcDaServer.3"), "ABB.AC800MC_OpcDaServer.3")
        self.host = _clean_str(self.opc_config.get("host", "localhost"), "localhost")

        # Simulation settings
        self.simulation_enabled = self.opc_config.get("simulation", {}).get("enabled", True)

        # Active driver references
        # 'opc_ua', 'opc_ua_sync', 'opc_da_openopc', 'opc_da_win32', 'opc_da_wow64', or 'simulation'
        self.active_protocol = None
        self.connected_da_server = None
        self.ua_client = None
        self.da_client = None
        self.da_com_server = None
        self.da_com_group = None
        self.da_com_items = {}
        self.da_com_server_handles = {}  # {config_tag: int(server_handle)}
        self.da_com_resolved_tags = {}   # {config_tag: actual_opc_tag}
        self.da_com_warned_tags = set()  # tags that already logged an unknown-item warning
        self.wow64_proc = None
        self.connected = False

        # Determine simulation mode
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
            logger.info(
                f"OPC Client initialized (Protocol: {self.protocol}, "
                f"DA Server: '{self.server_name}', UA Endpoint: '{self.endpoint_url}')"
            )

    def _is_windows_platform(self):
        return sys.platform == "win32"

    def _is_local_host(self):
        """Returns True if self.host refers to the local Windows machine."""
        if not self.host:
            return True
        h = self.host.strip().lower()
        if h in ("localhost", "127.0.0.1", ".", "(local)", "::1"):
            return True
        try:
            local_names = {
                socket.gethostname().lower(),
                socket.getfqdn().lower(),
                os.environ.get("COMPUTERNAME", "").lower(),
            }
            return h in local_names
        except Exception:
            return False

    def _has_opc_ua_library(self):
        """Checks whether asyncua or opcua is installed without noisy warnings."""
        for mod in ("asyncua", "opcua"):
            try:
                __import__(mod)
                return True
            except ImportError:
                continue
        return False

    def _candidate_tag_names(self, tag):
        """
        Generates candidate OPC DA ItemIDs for a configured tag.
        If the user prefixed the tag with the OPC Server ProgID (e.g.
        'ABB.AC800MC_OpcDaServer.3.Applications.Application_1.AI1.Value'),
        prioritizes the stripped ItemID ('Applications.Application_1.AI1.Value') first.
        """
        candidates = []
        prefixes = []
        if self.connected_da_server:
            prefixes.append(self.connected_da_server + ".")
        if self.server_name and (self.server_name + ".") not in prefixes:
            prefixes.append(self.server_name + ".")
        for alt in ("ABB.AC800MC_OpcDaServer.3.", "ABB.AC800MOPCDAServer.4.", "ABB.AC800MC_OpcDaServer."):
            if alt not in prefixes:
                prefixes.append(alt)

        for pfx in prefixes:
            if tag.lower().startswith(pfx.lower()):
                stripped = tag[len(pfx):]
                if stripped and stripped not in candidates:
                    candidates.append(stripped)
        if tag not in candidates:
            candidates.append(tag)
        return candidates

    def _format_opc_error(self, err):
        """Formats OPC / COM error codes into clear descriptions."""
        s = str(err)
        if "-1073479673" in s or "0xc0040007" in s.lower():
            return "OPC_E_UNKNOWNITEMID (-1073479673): Tag does not exist in the OPC Server address space"
        if "-1073479672" in s or "0xc0040008" in s.lower():
            return "OPC_E_INVALIDITEMID (-1073479672): Tag syntax is invalid"
        if "-2147467262" in s or "0x80004002" in s.lower():
            return "E_NOINTERFACE (-2147467262): COM interface marshaling error"
        return s

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
        On Windows, when protocol is 'auto' and an OPC DA server is configured, OPC DA is prioritized.
        """
        if self.simulation_mode:
            logger.info("Connecting to Simulated ABB OPC Server...")
            time.sleep(0.3)
            self.active_protocol = "simulation"
            self.connected = True
            logger.info("Connected to Simulated ABB OPC Server successfully.")
            return True

        try_da_first = (
            self._is_windows_platform()
            and self.protocol in ("opc_da", "da", "auto")
            and bool(self.server_name)
        )

        if try_da_first:
            try:
                if self._connect_opc_da():
                    return True
            except Exception as e:
                logger.warning(f"OPC DA connection attempt failed: {e}")
                if self.protocol in ("opc_da", "da"):
                    return self._fallback_simulation(f"OPC DA connection failed: {e}")

        if self.protocol in ("opc_ua", "ua") or (
            self.protocol == "auto" and str(self.endpoint_url).startswith("opc.tcp://") and self._has_opc_ua_library()
        ):
            try:
                if self._connect_opc_ua():
                    return True
            except Exception as e:
                logger.warning(f"OPC UA connection attempt failed: {e}")
                if self.protocol in ("opc_ua", "ua"):
                    return self._fallback_simulation(f"OPC UA connection failed: {e}")
        elif self.protocol == "auto" and not self._is_windows_platform():
            logger.warning("Neither 'asyncua' nor 'opcua' is installed and OPC DA requires Windows.")

        if not try_da_first and self.protocol in ("opc_da", "da", "auto") and self._is_windows_platform():
            try:
                if self._connect_opc_da():
                    return True
            except Exception as e:
                logger.warning(f"OPC DA connection attempt failed: {e}")
                if self.protocol in ("opc_da", "da"):
                    return self._fallback_simulation(f"OPC DA connection failed: {e}")

        return self._fallback_simulation("All live OPC connection attempts failed")

    def _connect_opc_ua(self):
        """Attempts connection to an OPC UA server."""
        logger.info(f"Attempting connection to OPC UA Server: {self.endpoint_url}")

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

    def _build_da_candidates(self):
        """Builds prioritized list of candidate OPC DA Server ProgIDs."""
        candidates = []
        if self.server_name:
            candidates.append(self.server_name)

        if "abb" in self.server_name.lower():
            abb_defaults = [
                "ABB.AC800MC_OpcDaServer.3",
                "ABB.AC800MOPCDAServer.4",
                "ABB.AC800MC_OpcDaServer",
                "ABB.AC800MC_OpcDaServer.2",
                "ABB.AC800MC_OpcDaServer.1",
                "ABB.AfwOpcDaSurrogate.1",
                "ABB.OpcDaServer.1",
                "ABB.DriveOPC",
                "ABB.DriveDA",
                "ABB.DriveOPC.1",
                "ABB.DriveDA.1",
            ]
            for alt in abb_defaults:
                if alt not in candidates:
                    candidates.append(alt)
        return candidates

    def _ensure_com_dll_surrogate(self):
        """
        Enables 32-bit COM DLL Surrogate and registers OPCDAAuto.dll TypeLib & Interface
        Proxy/Stubs (PSOAInterface) in HKCU so 64-bit Python can communicate with 32-bit
        OPCDAAuto.dll via dllhost.exe without E_NOINTERFACE errors.
        """
        if not self._is_windows_platform():
            return
        try:
            import winreg
            clsid = self.OPC_DA_AUTO_CLSID
            libid = self.OPC_DA_AUTO_LIBID
            psoa = self.PSOA_INTERFACE_CLSID
            syswow64_dll = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "SysWOW64", "OPCDAAuto.dll")

            # 1. Configure AppID DllSurrogate in HKCU\Software\Classes
            appid_path = rf"Software\Classes\AppID\{clsid}"
            with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, appid_path, 0, winreg.KEY_WRITE) as k:
                winreg.SetValueEx(k, "DllSurrogate", 0, winreg.REG_SZ, "")

            for clsid_base in (
                rf"Software\Classes\WOW6432Node\CLSID\{clsid}",
                rf"Software\Classes\CLSID\{clsid}",
            ):
                with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, clsid_base, 0, winreg.KEY_WRITE) as k:
                    winreg.SetValueEx(k, "AppID", 0, winreg.REG_SZ, clsid)
                if os.path.exists(syswow64_dll):
                    with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, rf"{clsid_base}\InprocServer32", 0, winreg.KEY_WRITE) as k:
                        winreg.SetValueEx(k, "", 0, winreg.REG_SZ, syswow64_dll)
                        winreg.SetValueEx(k, "ThreadingModel", 0, winreg.REG_SZ, "Both")
                    with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, rf"{clsid_base}\TypeLib", 0, winreg.KEY_WRITE) as k:
                        winreg.SetValueEx(k, "", 0, winreg.REG_SZ, libid)

            for progid in ("OPC.Automation", "OPC.Automation.1"):
                with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, rf"Software\Classes\{progid}\CLSID", 0, winreg.KEY_WRITE) as k:
                    winreg.SetValueEx(k, "", 0, winreg.REG_SZ, clsid)

            # 2. Register TypeLib and all OPCDAAuto.dll dual interfaces with PSOAInterface in 64-bit & 32-bit HKCU
            if os.path.exists(syswow64_dll):
                for arch in ("win32", "win64"):
                    tlb_key = rf"Software\Classes\TypeLib\{libid}\1.0\0\{arch}"
                    with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, tlb_key, 0, winreg.KEY_WRITE) as k:
                        winreg.SetValueEx(k, "", 0, winreg.REG_SZ, syswow64_dll)

                # OPCDAAuto.dll interfaces span {28E68F92-8D75-11D1-8DC3-3C302A000000} .. {28E68FA0-8D75-11D1-8DC3-3C302A000000}
                for low_byte in range(0x92, 0xA2):
                    iid = f"{{28E68F{low_byte:02X}-8D75-11D1-8DC3-3C302A000000}}"
                    for if_base in (
                        rf"Software\Classes\Interface\{iid}",
                        rf"Software\Classes\WOW6432Node\Interface\{iid}",
                    ):
                        with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, rf"{if_base}\ProxyStubClsid32", 0, winreg.KEY_WRITE) as k:
                            winreg.SetValueEx(k, "", 0, winreg.REG_SZ, psoa)
                        with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, rf"{if_base}\TypeLib", 0, winreg.KEY_WRITE) as k:
                            winreg.SetValueEx(k, "", 0, winreg.REG_SZ, libid)
                            winreg.SetValueEx(k, "Version", 0, winreg.REG_SZ, "1.0")
        except Exception as e:
            logger.debug(f"Could not configure HKCU COM DllSurrogate/TypeLib for OPCDAAuto.dll: {e}")

    def _dispatch_opc_automation_wrapper(self):
        """
        Attempts to instantiate an OPC DA Automation Wrapper COM object using win32com.
        Tries all known wrapper ProgIDs, standard CLSID, and 32-bit surrogate activation.
        Returns (com_server_obj, wrapper_name) or raises Exception with full diagnostics.
        """
        import pythoncom
        import win32com.client

        try:
            pythoncom.CoInitialize()
        except Exception:
            pass

        self._ensure_com_dll_surrogate()

        errors = []
        targets = list(self.DA_WRAPPER_PROGIDS) + [self.OPC_DA_AUTO_CLSID]
        for wrapper in targets:
            try:
                obj = win32com.client.Dispatch(wrapper)
                return obj, wrapper
            except Exception as ex:
                errors.append(f"{wrapper}: {ex}")

        CLSCTX_ACTIVATE_32_BIT_SERVER = 0x40000
        clsctx_flags = [
            pythoncom.CLSCTX_ALL,
            pythoncom.CLSCTX_LOCAL_SERVER,
            pythoncom.CLSCTX_LOCAL_SERVER | CLSCTX_ACTIVATE_32_BIT_SERVER,
            pythoncom.CLSCTX_INPROC_SERVER | pythoncom.CLSCTX_LOCAL_SERVER,
        ]
        for wrapper in ("OPC.Automation", "OPC.Automation.1", "Graybox.OPC.DAWrapper", self.OPC_DA_AUTO_CLSID):
            for ctx in clsctx_flags:
                try:
                    unk = pythoncom.CoCreateInstance(wrapper, None, ctx, pythoncom.IID_IDispatch)
                    obj = win32com.client.Dispatch(unk)
                    logger.info(f"Instantiated 32-bit OPC Automation wrapper '{wrapper}' via COM surrogate (ctx={hex(ctx)}).")
                    return obj, wrapper
                except Exception as ex:
                    errors.append(f"{wrapper}[ctx={hex(ctx)}]: {ex}")

        raise RuntimeError(
            "No registered OPC DA Automation Wrapper (OPCDAAuto.dll / Graybox / ABB) could be loaded in Python. "
            f"Details: {errors[:3]}"
        )

    def _connect_opc_da(self):
        """Attempts connection to an OPC DA server (e.g. ABB.AC800MC_OpcDaServer.3)."""
        is_local = self._is_local_host()
        logger.info(
            f"Attempting connection to OPC DA Server '{self.server_name}' "
            f"on host '{self.host}' (local={is_local})..."
        )

        try:
            import pythoncom
            pythoncom.CoInitialize()
        except Exception:
            pass

        candidates = self._build_da_candidates()
        diagnostic_errors = []

        # 1. Try OpenOPC if installed
        try:
            import OpenOPC
            for srv in candidates:
                try:
                    logger.debug(f"Trying OpenOPC with server ProgID '{srv}'...")
                    client = OpenOPC.client()
                    if is_local:
                        try:
                            client.connect(srv)
                        except Exception:
                            client.connect(srv, self.host)
                    else:
                        client.connect(srv, self.host)
                    self.da_client = client
                    self.connected_da_server = srv
                    self.active_protocol = "opc_da_openopc"
                    self.connected = True
                    logger.info(f"Successfully connected to OPC DA Server '{srv}' using OpenOPC.")
                    return True
                except Exception as ex:
                    diagnostic_errors.append(f"OpenOPC({srv}): {ex}")
        except ImportError:
            logger.debug("OpenOPC library not installed; using win32com / WOW64 COM bridge.")

        # 2. Try win32com.client with multi-wrapper detection and local COM connect
        try:
            import win32com.client  # noqa: F401
            com_srv, wrapper_used = self._dispatch_opc_automation_wrapper()
            logger.info(f"Loaded OPC DA Automation Wrapper: '{wrapper_used}'")

            try:
                discovered = com_srv.GetOPCServers() if is_local else com_srv.GetOPCServers(self.host)
                if discovered:
                    disc_list = [str(s) for s in discovered if s]
                    logger.info(f"Discovered registered OPC DA Servers on host: {disc_list}")
                    for d_srv in disc_list:
                        if d_srv.lower() == self.server_name.lower() and d_srv not in candidates:
                            candidates.insert(0, d_srv)
                        elif "abb" in d_srv.lower() and d_srv not in candidates:
                            candidates.append(d_srv)
            except Exception as disc_err:
                logger.debug(f"GetOPCServers() discovery note: {disc_err}")

            for srv in candidates:
                connect_calls = (
                    [(srv,), (srv, self.host), (srv, "127.0.0.1")]
                    if is_local
                    else [(srv, self.host), (srv,)]
                )
                for args in connect_calls:
                    try:
                        com_srv, _ = self._dispatch_opc_automation_wrapper()
                        com_srv.Connect(*args)
                        self.da_com_server = com_srv
                        self.da_com_group = None
                        self.da_com_items = {}
                        self.da_com_server_handles = {}
                        self.da_com_resolved_tags = {}
                        self.da_com_warned_tags = set()
                        self.connected_da_server = srv
                        self.active_protocol = "opc_da_win32"
                        self.connected = True
                        logger.info(
                            f"Successfully connected to OPC DA Server '{srv}' "
                            f"using win32com ({wrapper_used}, args={args})."
                        )
                        return True
                    except Exception as ex:
                        diagnostic_errors.append(f"win32com({srv}, args={args}): {ex}")
        except ImportError:
            diagnostic_errors.append("pywin32 (win32com.client) is not installed")
        except Exception as wrapper_err:
            diagnostic_errors.append(str(wrapper_err))

        # 3. Try 32-bit WOW64 PowerShell COM Bridge
        if self._connect_opc_da_wow64_bridge(candidates, is_local, diagnostic_errors):
            return True

        err_summary = " | ".join(diagnostic_errors[:5])
        logger.error(
            f"OPC DA connection failed for candidates {candidates}. "
            f"Diagnostics: {err_summary}. "
            "Troubleshooting: 1) Ensure OPCDAAuto.dll is registered ('C:\\Windows\\SysWOW64\\regsvr32.exe OPCDAAuto.dll'), "
            "2) Ensure ABB OPC DA Server service is running, "
            "3) Run terminal/application as Administrator if DCOM permissions are restricted."
        )
        raise ConnectionError(f"Could not connect to any OPC DA candidate servers: {candidates}. ({err_summary})")

    def _connect_opc_da_wow64_bridge(self, candidates, is_local, diagnostic_errors):
        """
        Spawns a persistent 32-bit Windows PowerShell (SysWOW64) worker using C# Reflection
        with 1-based SAFEARRAYs to call OPCItems.AddItems and OPCGroup.SyncRead in-process,
        completely avoiding COPCItem (E_NOINTERFACE -2147467262).
        """
        sys_root = os.environ.get("SystemRoot", r"C:\Windows")
        ps32_path = os.path.join(sys_root, "SysWOW64", "WindowsPowerShell", "v1.0", "powershell.exe")
        if not os.path.exists(ps32_path):
            ps32_path = "powershell.exe"

        ps_script = r'''
$ErrorActionPreference = 'Stop'
Add-Type -TypeDefinition @"
using System;
using System.Reflection;

public static class OpcDaSyncHelper {
    public static int AddSingleItem(object opcItems, string itemId, int clientHandle, out int errCode) {
        errCode = -1;
        Array itemIds = Array.CreateInstance(typeof(string), new int[] { 1 }, new int[] { 1 });
        itemIds.SetValue(itemId, 1);
        Array clientHandles = Array.CreateInstance(typeof(int), new int[] { 1 }, new int[] { 1 });
        clientHandles.SetValue(clientHandle, 1);
        Array serverHandles = null;
        Array errors = null;

        object[] args = new object[] { 1, itemIds, clientHandles, serverHandles, errors, Type.Missing, Type.Missing };
        ParameterModifier pMod = new ParameterModifier(7);
        pMod[3] = true;
        pMod[4] = true;

        opcItems.GetType().InvokeMember(
            "AddItems",
            BindingFlags.InvokeMethod,
            null,
            opcItems,
            args,
            new ParameterModifier[] { pMod },
            null,
            null
        );

        serverHandles = (Array)args[3];
        errors = (Array)args[4];
        if (errors != null && errors.Length >= 1) {
            int lb = errors.GetLowerBound(0);
            errCode = Convert.ToInt32(errors.GetValue(lb));
        }
        if (errCode == 0 && serverHandles != null && serverHandles.Length >= 1) {
            int lb = serverHandles.GetLowerBound(0);
            return Convert.ToInt32(serverHandles.GetValue(lb));
        }
        return 0;
    }

    public static object[] SyncReadItems(object opcGroup, int[] srvHandles, out int[] errCodes) {
        int n = srvHandles.Length;
        errCodes = new int[n];
        object[] resultVals = new object[n];
        if (n == 0) return resultVals;

        Array handlesArr = Array.CreateInstance(typeof(int), new int[] { n }, new int[] { 1 });
        for (int i = 0; i < n; i++) {
            handlesArr.SetValue(srvHandles[i], i + 1);
        }
        Array values = null;
        Array errors = null;
        object qualities = Type.Missing;
        object timestamps = Type.Missing;

        object[] args = new object[] { (short)2, n, handlesArr, values, errors, qualities, timestamps };
        ParameterModifier pMod = new ParameterModifier(7);
        pMod[3] = true;
        pMod[4] = true;

        opcGroup.GetType().InvokeMember(
            "SyncRead",
            BindingFlags.InvokeMethod,
            null,
            opcGroup,
            args,
            new ParameterModifier[] { pMod },
            null,
            null
        );

        values = (Array)args[3];
        errors = (Array)args[4];
        int vLb = values != null ? values.GetLowerBound(0) : 1;
        int eLb = errors != null ? errors.GetLowerBound(0) : 1;
        for (int i = 0; i < n; i++) {
            if (values != null && i < values.Length) resultVals[i] = values.GetValue(vLb + i);
            if (errors != null && i < errors.Length) errCodes[i] = Convert.ToInt32(errors.GetValue(eLb + i));
        }
        return resultVals;
    }
}
"@

$wrappers = @('OPC.Automation', 'OPC.Automation.1', 'Graybox.OPC.DAWrapper', 'Graybox.OPC.DAWrapper.1', 'Matrikon.OPC.Automation', 'ABB.OPC.Automation', 'RSI.OPCAutomation', 'HSCOPC.Automation')
$opc = $null
$usedWrapper = $null
foreach ($w in $wrappers) {
    try {
        $opc = New-Object -ComObject $w
        $usedWrapper = $w
        break
    } catch {}
}
if ($null -eq $opc) {
    Write-Output (@{ status = 'error'; error = '32-bit PowerShell could not instantiate OPC.Automation' } | ConvertTo-Json -Compress)
    exit 1
}

$candidates = $env:OPC_CANDIDATES_CSV -split '\|'
$hostName = $env:OPC_HOST
$isLocal = ($env:OPC_IS_LOCAL -eq '1')
$connectedServer = $null
$connErrors = @()

foreach ($srv in $candidates) {
    if (-not $srv) { continue }
    try {
        if ($isLocal) {
            try { $opc.Connect($srv) } catch { $opc.Connect($srv, $hostName) }
        } else {
            $opc.Connect($srv, $hostName)
        }
        $connectedServer = $srv
        break
    } catch {
        $connErrors += "$srv : $($_.Exception.Message)"
    }
}

if ($null -eq $connectedServer) {
    Write-Output (@{ status = 'error'; error = ($connErrors -join '; ') } | ConvertTo-Json -Compress)
    exit 1
}

$group = $opc.OPCGroups.Add('ReportGenWow64Group')
$group.IsActive = $true
$opcItems = $group.OPCItems
$handlesMap = @{}
$handleSeq = 1
$stripPrefix = ($connectedServer + '.').ToLower()

Write-Output (@{ status = 'connected'; wrapper = $usedWrapper; server = $connectedServer } | ConvertTo-Json -Compress)

while ($true) {
    $line = [Console]::In.ReadLine()
    if ($null -eq $line -or $line.Trim() -eq 'QUIT' -or $line.Trim() -eq '') { break }
    if ($line.StartsWith('READ:')) {
        $tags = $line.Substring(5) -split '\|'
        $valsOut = @{}
        $errsOut = @{}
        $validTags = New-Object System.Collections.Generic.List[string]
        $validHandles = New-Object System.Collections.Generic.List[int]

        foreach ($t in $tags) {
            if (-not $t) { continue }
            if (-not $handlesMap.ContainsKey($t)) {
                $candList = New-Object System.Collections.Generic.List[string]
                if ($t.ToLower().StartsWith($stripPrefix)) {
                    $candList.Add($t.Substring($stripPrefix.Length))
                }
                $candList.Add($t)
                $addedHandle = 0
                $lastErr = -1
                foreach ($c in $candList) {
                    try {
                        $errCode = 0
                        $h = [OpcDaSyncHelper]::AddSingleItem($opcItems, $c, $handleSeq, [ref]$errCode)
                        if ($errCode -eq 0 -and $h -ne 0) {
                            $addedHandle = $h
                            $handleSeq += 1
                            break
                        }
                        $lastErr = $errCode
                    } catch {
                        $lastErr = $_.Exception.Message
                    }
                }
                if ($addedHandle -ne 0) {
                    $handlesMap[$t] = $addedHandle
                } else {
                    $valsOut[$t] = $null
                    $errsOut[$t] = "$lastErr"
                }
            }
            if ($handlesMap.ContainsKey($t)) {
                $validTags.Add($t)
                $validHandles.Add([int]$handlesMap[$t])
            }
        }

        if ($validTags.Count -gt 0) {
            try {
                $readErrs = $null
                $readVals = [OpcDaSyncHelper]::SyncReadItems($group, $validHandles.ToArray(), [ref]$readErrs)
                for ($i = 0; $i -lt $validTags.Count; $i++) {
                    $tagKey = $validTags[$i]
                    if ($readErrs[$i] -eq 0) {
                        $valsOut[$tagKey] = $readVals[$i]
                    } else {
                        $valsOut[$tagKey] = $null
                        $errsOut[$tagKey] = "SyncRead error $($readErrs[$i])"
                    }
                }
            } catch {
                foreach ($tagKey in $validTags) {
                    $valsOut[$tagKey] = $null
                    $errsOut[$tagKey] = $_.Exception.Message
                }
            }
        }
        Write-Output (@{ status = 'ok'; values = $valsOut; errors = $errsOut } | ConvertTo-Json -Compress)
    }
}
try { $opc.Disconnect() } catch {}
'''
        try:
            logger.info("Starting 32-bit WOW64 in-process OPC DA SyncRead bridge...")
            env = os.environ.copy()
            env["OPC_CANDIDATES_CSV"] = "|".join(candidates)
            env["OPC_HOST"] = self.host
            env["OPC_IS_LOCAL"] = "1" if is_local else "0"

            creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
            proc = subprocess.Popen(
                [ps32_path, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command", ps_script],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                env=env,
                creationflags=creationflags,
            )
            first_line = proc.stdout.readline()
            if not first_line:
                stderr_out = proc.stderr.read().strip()
                proc.terminate()
                diagnostic_errors.append(f"WOW64Bridge: {stderr_out or 'No output from 32-bit PowerShell'}")
                return False

            resp = json.loads(first_line.strip())
            if resp.get("status") == "connected":
                self.wow64_proc = proc
                self.connected_da_server = resp.get("server") or self.server_name
                self.active_protocol = "opc_da_wow64"
                self.connected = True
                logger.info(
                    f"Successfully connected to OPC DA Server '{resp.get('server')}' "
                    f"via 32-bit WOW64 In-Process SyncRead Bridge (Wrapper: {resp.get('wrapper')})."
                )
                return True
            else:
                proc.terminate()
                diagnostic_errors.append(f"WOW64Bridge: {resp.get('error')}")
                return False
        except Exception as e:
            diagnostic_errors.append(f"WOW64Bridge exception: {e}")
            return False

    def disconnect(self):
        """Disconnects from the active OPC server."""
        if self.simulation_mode:
            logger.info("Disconnected from Simulated OPC Server.")
            self.connected = False
            self.active_protocol = None
            return

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

        if self.da_client:
            try:
                self.da_client.close()
                logger.info("OpenOPC connection closed.")
            except Exception as e:
                logger.error(f"Error closing OpenOPC client: {e}")
            finally:
                self.da_client = None

        if self.da_com_server:
            try:
                if self.da_com_group is not None:
                    try:
                        self.da_com_server.OPCGroups.Remove("ReportGenReadGroup")
                    except Exception:
                        pass
                self.da_com_server.Disconnect()
                logger.info("win32com OPC connection closed.")
            except Exception as e:
                logger.error(f"Error disconnecting win32com client: {e}")
            finally:
                self.da_com_server = None
                self.da_com_group = None
                self.da_com_items = {}
                self.da_com_server_handles = {}
                self.da_com_resolved_tags = {}

        if self.wow64_proc:
            try:
                if self.wow64_proc.stdin:
                    self.wow64_proc.stdin.write("QUIT\n")
                    self.wow64_proc.stdin.flush()
                self.wow64_proc.terminate()
                logger.info("32-bit WOW64 OPC DA bridge closed.")
            except Exception as e:
                logger.error(f"Error closing WOW64 OPC bridge: {e}")
            finally:
                self.wow64_proc = None

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

        if self.active_protocol == "opc_ua":
            return self._read_tags_opc_ua_async(tags)
        elif self.active_protocol == "opc_ua_sync":
            return self._read_tags_opc_ua_sync(tags)

        if self.active_protocol == "opc_da_openopc" and self.da_client:
            return self._read_tags_openopc(tags)

        if self.active_protocol == "opc_da_win32" and self.da_com_server:
            return self._read_tags_win32com(tags)

        if self.active_protocol == "opc_da_wow64" and self.wow64_proc:
            return self._read_tags_wow64(tags)

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

    def _add_item_handle_win32com(self, opc_items, tag, client_handle):
        """
        Adds a tag to OPCItems using OPCItems.AddItems (via InvokeTypes) instead of AddItem.
        Returns (server_handle, resolved_tag) or raises the underlying COM error.
        Because AddItems returns SAFEARRAY(long) ServerHandles instead of a COPCItem pointer,
        it completely avoids the OPCDAAuto.dll COPCItem E_NOINTERFACE (-2147467262) bug!
        """
        import pythoncom
        dispid_add_items = opc_items._oleobj_.GetIDsOfNames("AddItems")
        last_err = None

        for candidate in self._candidate_tag_names(tag):
            try:
                # Note: pywin32 InvokeTypes requires an argument for every entry in ADD_ITEMS_ARG_TYPES (7 total),
                # passing pythoncom.Missing for the two [out] parameters (ServerHandles, Errors) and two [in, optional] parameters.
                res = opc_items._oleobj_.InvokeTypes(
                    dispid_add_items,
                    0,
                    1,  # pythoncom.DISPATCH_METHOD
                    (24, 0),
                    self.ADD_ITEMS_ARG_TYPES,
                    1,
                    ["", candidate],
                    [0, int(client_handle)],
                    pythoncom.Missing,
                    pythoncom.Missing,
                    pythoncom.Missing,
                    pythoncom.Missing,
                )
                if isinstance(res, tuple) and len(res) >= 2:
                    srv_handles, errs = res[0], res[1]
                    err_code = int(errs[-1]) if errs else -1
                    srv_handle = int(srv_handles[-1]) if srv_handles else 0
                    if err_code == 0 and srv_handle != 0:
                        if candidate != tag:
                            logger.info(f"Auto-resolved prefixed OPC DA tag '{tag}' -> '{candidate}'")
                        return srv_handle, candidate
                    last_err = f"OPC Error Code {err_code} ({self._format_opc_error(err_code)})"
            except Exception as ex:
                last_err = ex

        raise RuntimeError(last_err or f"Could not add OPC DA tag '{tag}'")

    def _sync_read_handles_win32com(self, opc_group, server_handles):
        """
        Calls OPCGroup.SyncRead via InvokeTypes for a list of integer server_handles.
        Returns (values_list, errors_list). Never touches COPCItem interfaces.
        """
        import pythoncom
        dispid_sync_read = opc_group._oleobj_.GetIDsOfNames("SyncRead")
        num_items = len(server_handles)
        handles_arr = [0] + [int(h) for h in server_handles]

        # Try OPCDevice (2) first, fallback to OPCCache (1)
        last_err = None
        for source in (2, 1):
            try:
                # Pass all 7 positional args matching SYNC_READ_ARG_TYPES (including pythoncom.Missing for the 4 [out] args)
                res = opc_group._oleobj_.InvokeTypes(
                    dispid_sync_read,
                    0,
                    1,  # pythoncom.DISPATCH_METHOD
                    (24, 0),
                    self.SYNC_READ_ARG_TYPES,
                    source,
                    num_items,
                    handles_arr,
                    pythoncom.Missing,
                    pythoncom.Missing,
                    pythoncom.Missing,
                    pythoncom.Missing,
                )
                if isinstance(res, tuple) and len(res) >= 2:
                    raw_vals = list(res[0]) if res[0] is not None else []
                    raw_errs = list(res[1]) if res[1] is not None else []
                    vals = raw_vals[-num_items:] if len(raw_vals) >= num_items else raw_vals
                    errs = raw_errs[-num_items:] if len(raw_errs) >= num_items else raw_errs
                    return vals, errs
            except Exception as ex:
                last_err = ex

        raise RuntimeError(f"OPCGroup.SyncRead failed: {last_err}")

    def _read_tags_win32com(self, tags):
        try:
            try:
                import pythoncom
                pythoncom.CoInitialize()
            except Exception:
                pass

            if self.da_com_group is None:
                try:
                    self.da_com_server.OPCGroups.Remove("ReportGenReadGroup")
                except Exception:
                    pass
                grp = self.da_com_server.OPCGroups.Add("ReportGenReadGroup")
                grp.IsActive = True
                self.da_com_group = grp
                self.da_com_items = {}
                self.da_com_server_handles = {}
                self.da_com_resolved_tags = {}

            opc_items = self.da_com_group.OPCItems
            result = {t: None for t in tags}

            # 1. Register any new tags via OPCItems.AddItems (returns integer ServerHandles, no COPCItem)
            for idx, t in enumerate(tags):
                if t in self.da_com_server_handles:
                    continue
                client_handle = len(self.da_com_server_handles) + idx + 1
                try:
                    srv_handle, resolved = self._add_item_handle_win32com(opc_items, t, client_handle)
                    self.da_com_server_handles[t] = srv_handle
                    self.da_com_resolved_tags[t] = resolved
                except Exception as add_err:
                    err_str = str(add_err)
                    # If cross-bitness COM surrogate cannot marshal SAFEARRAYs (-2147467262), switch to 32-bit WOW64 in-process bridge
                    if "-2147467262" in err_str or "0x80004002" in err_str.lower():
                        logger.warning(
                            "64-bit/32-bit COM surrogate hit E_NOINTERFACE (-2147467262). "
                            "Switching to 32-bit WOW64 in-process OPC DA bridge..."
                        )
                        diag = []
                        if self._connect_opc_da_wow64_bridge(
                            [self.connected_da_server or self.server_name],
                            self._is_local_host(),
                            diag,
                        ):
                            return self._read_tags_wow64(tags)
                    if t not in self.da_com_warned_tags:
                        logger.warning(f"OPC DA tag '{t}' could not be added: {self._format_opc_error(add_err)}")
                        self.da_com_warned_tags.add(t)

            # 2. Perform bulk synchronous read via OPCGroup.SyncRead using integer ServerHandles
            valid_tags = [t for t in tags if t in self.da_com_server_handles]
            if not valid_tags:
                return result

            valid_handles = [self.da_com_server_handles[t] for t in valid_tags]
            try:
                vals, errs = self._sync_read_handles_win32com(self.da_com_group, valid_handles)
                for idx, t in enumerate(valid_tags):
                    err_code = int(errs[idx]) if idx < len(errs) else 0
                    val = vals[idx] if idx < len(vals) else None
                    if err_code == 0:
                        result[t] = val
                    else:
                        logger.warning(f"SyncRead error for tag '{t}': {self._format_opc_error(err_code)}")
                        result[t] = None
            except Exception as sync_err:
                err_str = str(sync_err)
                if "-2147467262" in err_str or "0x80004002" in err_str.lower():
                    logger.warning("SyncRead hit E_NOINTERFACE (-2147467262); switching to 32-bit WOW64 in-process bridge...")
                    diag = []
                    if self._connect_opc_da_wow64_bridge(
                        [self.connected_da_server or self.server_name],
                        self._is_local_host(),
                        diag,
                    ):
                        return self._read_tags_wow64(tags)
                raise sync_err

            return result
        except Exception as e:
            logger.error(f"win32com read error: {e}")
            self.da_com_group = None
            self.da_com_items = {}
            self.da_com_server_handles = {}
            raise e

    def _read_tags_wow64(self, tags):
        if not self.wow64_proc or self.wow64_proc.poll() is not None:
            raise RuntimeError("32-bit WOW64 OPC DA bridge process is not running.")
        req_line = "READ:" + "|".join(str(t) for t in tags)
        self.wow64_proc.stdin.write(req_line + "\n")
        self.wow64_proc.stdin.flush()
        line = self.wow64_proc.stdout.readline()
        if not line:
            raise RuntimeError("No response from 32-bit WOW64 OPC DA bridge.")
        resp = json.loads(line.strip())
        if resp.get("status") != "ok":
            raise RuntimeError(f"WOW64 OPC read failed: {resp.get('error')}")
        errs = resp.get("errors") or {}
        for t, err_msg in errs.items():
            if t not in self.da_com_warned_tags:
                logger.warning(f"OPC DA tag '{t}' read warning: {self._format_opc_error(err_msg)}")
                self.da_com_warned_tags.add(t)
        return resp.get("values", {})

    # =========================================================================
    # Simulation Engine Methods
    # =========================================================================

    def _get_configured_trigger_tags(self):
        """Returns list of all configured BOOL trigger tags for simulation."""
        raw = self.opc_config.get("trigger_tags")
        tags = []
        if isinstance(raw, dict):
            for _, v in raw.items():
                if v and str(v).strip() and str(v).strip() not in tags:
                    tags.append(str(v).strip())
        elif isinstance(raw, list):
            for v in raw:
                if v and str(v).strip() and str(v).strip() not in tags:
                    tags.append(str(v).strip())
        single = self.opc_config.get("trigger_tag")
        if single and str(single).strip() and str(single).strip() not in tags:
            tags.append(str(single).strip())
        if not tags:
            tags = ["BatchCompleted", "BinLevelFull"]
        return tags

    def _update_simulation(self):
        """Simulates progress of a physical batching process including rising-edge BOOL triggers."""
        now = time.time()
        elapsed = now - self.sim_last_update
        trigger_tags = self._get_configured_trigger_tags()

        if elapsed >= self.sim_interval:
            self.sim_batch_no += 1
            self.sim_last_update = now
            self.sim_tags_cache = self._generate_simulated_tags(trigger_high=True)
            logger.info(f"[OPC Simulation] New batch generated! Batch No: {self.sim_batch_no}")
        else:
            # Reset trigger tags to LOW (False) after trigger pulse window
            for t in trigger_tags:
                self.sim_tags_cache[t] = False

    def _generate_simulated_tags(self, trigger_high=False):
        """Generates realistic random values for simulated ABB drive tags."""
        mappings = self.opc_config.get("tag_mappings", {})
        trigger_tags = self._get_configured_trigger_tags()

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
        }

        for t in trigger_tags:
            sim_vals[t] = False

        if trigger_high and trigger_tags:
            active_tag = random.choice(trigger_tags)
            sim_vals[active_tag] = True

        return sim_vals
