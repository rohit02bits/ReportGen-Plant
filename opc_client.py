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
    1. Classic OPC DA (Windows COM/DCOM via OpenOPC, win32com, or 32-bit WOW64 COM Bridge, e.g. ABB.AC800MC_OpcDaServer.3 / ABB.DriveOPC)
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

    # Standard OPC Foundation OPCDAAuto.dll CLSID
    OPC_DA_AUTO_CLSID = "{28E68F9A-8D75-11D1-8DC3-3C302A000000}"

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
        self.ua_client = None
        self.da_client = None
        self.da_com_server = None
        self.da_com_group = None
        self.da_com_items = {}
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

        # Determine connection order:
        # On Windows, if protocol is 'opc_da'/'da' or ('auto' with a DA server configured), try OPC DA first!
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

        # Try OPC UA if requested explicitly, or in 'auto' mode when an OPC UA library is actually installed
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

        # If DA wasn't tried yet (e.g. non-standard config on Windows), try it now
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

    def _build_da_candidates(self):
        """Builds prioritized list of candidate OPC DA Server ProgIDs."""
        candidates = []
        if self.server_name:
            candidates.append(self.server_name)

        if "abb" in self.server_name.lower():
            abb_defaults = [
                "ABB.AC800MC_OpcDaServer.3",
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
        Enables 32-bit COM DLL Surrogate in HKCU for OPCDAAuto.dll so 64-bit Python
        can instantiate 32-bit OPCDAAuto.dll via Windows dllhost.exe without needing 32-bit Python.
        Does not require Administrator privileges (writes to HKCU\\Software\\Classes).
        """
        if not self._is_windows_platform():
            return
        try:
            import winreg
            clsid = self.OPC_DA_AUTO_CLSID
            # Check if 32-bit OPCDAAuto.dll exists on the system
            syswow64_dll = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "SysWOW64", "OPCDAAuto.dll")

            # Configure AppID DllSurrogate in HKCU\Software\Classes
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

            for progid in ("OPC.Automation", "OPC.Automation.1"):
                with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, rf"Software\Classes\{progid}\CLSID", 0, winreg.KEY_WRITE) as k:
                    winreg.SetValueEx(k, "", 0, winreg.REG_SZ, clsid)
        except Exception as e:
            logger.debug(f"Could not configure HKCU COM DllSurrogate for OPCDAAuto.dll: {e}")

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

        errors = []
        # 1. Try standard Dispatch across known wrapper ProgIDs + CLSID
        targets = list(self.DA_WRAPPER_PROGIDS) + [self.OPC_DA_AUTO_CLSID]
        for wrapper in targets:
            try:
                obj = win32com.client.Dispatch(wrapper)
                return obj, wrapper
            except Exception as ex:
                errors.append(f"{wrapper}: {ex}")

        # 2. If 64-bit Python hit Class Not Registered (0x80040154), enable HKCU DllSurrogate and retry with CLSCTX_LOCAL_SERVER
        self._ensure_com_dll_surrogate()
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

            # Discover installed OPC DA servers via GetOPCServers() if supported
            try:
                discovered = com_srv.GetOPCServers() if is_local else com_srv.GetOPCServers(self.host)
                if discovered:
                    disc_list = [str(s) for s in discovered if s]
                    logger.info(f"Discovered registered OPC DA Servers on host: {disc_list}")
                    # Add any discovered server matching configured server or ABB
                    for d_srv in disc_list:
                        if d_srv.lower() == self.server_name.lower() and d_srv not in candidates:
                            candidates.insert(0, d_srv)
                        elif "abb" in d_srv.lower() and d_srv not in candidates:
                            candidates.append(d_srv)
            except Exception as disc_err:
                logger.debug(f"GetOPCServers() discovery note: {disc_err}")

            for srv in candidates:
                # Important: For local OPC DA servers (like ABB.AC800MC_OpcDaServer.3),
                # calling Connect(srv) WITHOUT the second host argument uses local COM activation
                # and avoids remote DCOM / OpcEnum failures on 'localhost'.
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

        # 3. Try 32-bit WOW64 PowerShell COM Bridge (solves 64-bit Python vs 32-bit OPCDAAuto.dll / 32-bit ABB OPC Server)
        if self._connect_opc_da_wow64_bridge(candidates, is_local, diagnostic_errors):
            return True

        # Log clear, actionable diagnostics so the exact COM error is visible at WARNING/ERROR level
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
        Spawns a persistent 32-bit Windows PowerShell (SysWOW64) worker to bridge 64-bit Python
        to 32-bit OPCDAAuto.dll and 32-bit ABB OPC DA servers seamlessly.
        """
        sys_root = os.environ.get("SystemRoot", r"C:\Windows")
        ps32_path = os.path.join(sys_root, "SysWOW64", "WindowsPowerShell", "v1.0", "powershell.exe")
        if not os.path.exists(ps32_path):
            ps32_path = "powershell.exe"

        ps_script = r"""
$ErrorActionPreference = 'Stop'
$wrappers = @('OPC.Automation', 'OPC.Automation.1', 'Graybox.OPC.DAWrapper', 'Graybox.OPC.DAWrapper.1', 'Matrikon.OPC.Automation', 'ABB.OPC.Automation', 'RSI.OPCAutomation', 'HSCOPC.Automation')
$opc = $null
$usedWrapper = $null
$wrapErrors = @()
foreach ($w in $wrappers) {
    try {
        $opc = New-Object -ComObject $w
        $usedWrapper = $w
        break
    } catch {
        $wrapErrors += "$w : $($_.Exception.Message)"
    }
}
if ($null -eq $opc) {
    $out = @{ status = 'error'; error = "32-bit COM could not load any OPC Automation Wrapper (OPCDAAuto.dll not registered in SysWOW64). Details: " + ($wrapErrors -join '; ') }
    Write-Output ($out | ConvertTo-Json -Compress)
    exit 1
}

$discovered = @()
try {
    $disc = $opc.GetOPCServers()
    if ($null -ne $disc) {
        foreach ($d in $disc) { if ($d) { $discovered += [string]$d } }
    }
} catch {}

$candidatesJson = $env:OPC_CANDIDATES_JSON
$candidates = $candidatesJson | ConvertFrom-Json
foreach ($d in $discovered) {
    if ($d -like '*ABB*' -and ($candidates -notcontains $d)) {
        $candidates += $d
    }
}

$hostName = $env:OPC_HOST
$isLocal = ($env:OPC_IS_LOCAL -eq '1')
$connectedServer = $null
$connErrors = @()

foreach ($srv in $candidates) {
    try {
        if ($isLocal) {
            try {
                $opc.Connect($srv)
                $connectedServer = $srv
                break
            } catch {
                $opc.Connect($srv, $hostName)
                $connectedServer = $srv
                break
            }
        } else {
            $opc.Connect($srv, $hostName)
            $connectedServer = $srv
            break
        }
    } catch {
        $connErrors += "$srv : $($_.Exception.Message)"
    }
}

if ($null -eq $connectedServer) {
    $out = @{
        status = 'error'
        error = "32-bit wrapper '$usedWrapper' loaded (Discovered servers: $($discovered -join ', ')), but Connect failed: " + ($connErrors -join '; ')
    }
    Write-Output ($out | ConvertTo-Json -Compress)
    exit 1
}

$group = $opc.OPCGroups.Add('ReportGenWow64Group')
$group.IsActive = $true
$itemsMap = @{}
$handleSeq = 1

$initOut = @{
    status = 'connected'
    wrapper = $usedWrapper
    server = $connectedServer
    discovered = $discovered
}
Write-Output ($initOut | ConvertTo-Json -Compress)

while ($true) {
    $line = [Console]::In.ReadLine()
    if ($null -eq $line) { break }
    $line = $line.Trim()
    if ($line -eq '' -or $line -eq 'QUIT') { break }
    try {
        $req = $line | ConvertFrom-Json
        if ($req.cmd -eq 'READ') {
            $vals = @{}
            $errs = @{}
            foreach ($t in $req.tags) {
                $tagStr = [string]$t
                try {
                    if (-not $itemsMap.ContainsKey($tagStr)) {
                        $item = $group.OPCItems.AddItem($tagStr, $handleSeq)
                        $handleSeq += 1
                        $itemsMap[$tagStr] = $item
                    }
                    $it = $itemsMap[$tagStr]
                    try { $it.Read(2) | Out-Null } catch {}
                    $vals[$tagStr] = $it.Value
                } catch {
                    $vals[$tagStr] = $null
                    $errs[$tagStr] = $_.Exception.Message
                }
            }
            $resp = @{ status = 'ok'; values = $vals; errors = $errs }
            Write-Output ($resp | ConvertTo-Json -Compress)
        }
    } catch {
        $resp = @{ status = 'error'; error = $_.Exception.Message }
        Write-Output ($resp | ConvertTo-Json -Compress)
    }
}
try { $opc.Disconnect() } catch {}
"""
        try:
            logger.info("Trying 32-bit WOW64 OPC DA COM bridge for 64-bit Python compatibility...")
            env = os.environ.copy()
            env["OPC_CANDIDATES_JSON"] = json.dumps(candidates)
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
                self.active_protocol = "opc_da_wow64"
                self.connected = True
                logger.info(
                    f"Successfully connected to OPC DA Server '{resp.get('server')}' "
                    f"via 32-bit WOW64 COM Bridge (Wrapper: {resp.get('wrapper')}, "
                    f"Discovered: {resp.get('discovered')})."
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

        # Disconnect 32-bit WOW64 bridge
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

        # OPC DA Read via 32-bit WOW64 COM Bridge
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

            opc_items = self.da_com_group.OPCItems
            result = {}

            for idx, t in enumerate(tags):
                try:
                    if t not in self.da_com_items:
                        client_handle = len(self.da_com_items) + idx + 1
                        self.da_com_items[t] = opc_items.AddItem(t, client_handle)
                    item = self.da_com_items[t]
                    # Trigger synchronous device read (OPCDevice = 2); updates item.Value or returns tuple
                    try:
                        read_res = item.Read(2)
                        if isinstance(read_res, tuple) and len(read_res) >= 1 and read_res[0] is not None:
                            result[t] = read_res[0]
                            continue
                    except Exception:
                        pass
                    result[t] = item.Value
                except Exception as item_err:
                    logger.warning(f"win32com error reading OPC DA tag '{t}': {item_err}")
                    result[t] = None

            return result
        except Exception as e:
            logger.error(f"win32com read error: {e}")
            self.da_com_group = None
            self.da_com_items = {}
            raise e

    def _read_tags_wow64(self, tags):
        if not self.wow64_proc or self.wow64_proc.poll() is not None:
            raise RuntimeError("32-bit WOW64 OPC DA bridge process is not running.")
        req = json.dumps({"cmd": "READ", "tags": list(tags)})
        self.wow64_proc.stdin.write(req + "\n")
        self.wow64_proc.stdin.flush()
        line = self.wow64_proc.stdout.readline()
        if not line:
            raise RuntimeError("No response from 32-bit WOW64 OPC DA bridge.")
        resp = json.loads(line.strip())
        if resp.get("status") != "ok":
            raise RuntimeError(f"WOW64 OPC read failed: {resp.get('error')}")
        errs = resp.get("errors") or {}
        for t, err_msg in errs.items():
            logger.warning(f"OPC DA tag '{t}' read warning: {err_msg}")
        return resp.get("values", {})

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
