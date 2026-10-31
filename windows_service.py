"""
Windows Service wrapper for the Plant Batching Report Generator.

Runs the background scheduler (the same loop as `main.py --service`) under the
Windows Service Control Manager so it starts automatically at boot and is
restarted by Windows if it crashes (see install_service.bat for the recovery
settings).

Usage (from an elevated / Administrator prompt):
  ReportGeneratorService.exe install      # or: python windows_service.py install
  ReportGeneratorService.exe start
  ReportGeneratorService.exe stop
  ReportGeneratorService.exe remove
"""
import os
import sys
import logging
import threading

import servicemanager
import win32event
import win32service
import win32serviceutil

from main import get_base_path, setup_logging, load_config

SERVICE_NAME = "PlantReportGenerator"
SERVICE_DISPLAY_NAME = "Plant Batching Report Generator"
SERVICE_DESCRIPTION = "Generates shift-wise and daily plant batching Excel reports on the schedule defined in config.yaml."


class ReportGeneratorService(win32serviceutil.ServiceFramework):
    _svc_name_ = SERVICE_NAME
    _svc_display_name_ = SERVICE_DISPLAY_NAME
    _svc_description_ = SERVICE_DESCRIPTION

    def __init__(self, args):
        super().__init__(args)
        self.stop_requested = threading.Event()
        self.hWaitStop = win32event.CreateEvent(None, 0, 0, None)
        self.logger = logging.getLogger("ReportGenerator.Service")

    def SvcStop(self):
        self.ReportServiceStatus(win32service.SERVICE_STOP_PENDING)
        self.logger.info("Stop requested by Service Control Manager.")
        self.stop_requested.set()
        win32event.SetEvent(self.hWaitStop)

    def SvcDoRun(self):
        # Services start in C:\Windows\System32, so anchor everything to the app folder
        base_dir = get_base_path()
        os.chdir(base_dir)
        setup_logging(base_dir)

        servicemanager.LogMsg(
            servicemanager.EVENTLOG_INFORMATION_TYPE,
            servicemanager.PYS_SERVICE_STARTED,
            (self._svc_name_, ""),
        )
        self.logger.info(f"Service starting from: {base_dir}")

        worker = threading.Thread(target=self._run_scheduler, name="SchedulerWorker", daemon=True)
        worker.start()

        # Wait until either SCM asks us to stop or the worker thread dies
        while worker.is_alive():
            if win32event.WaitForSingleObject(self.hWaitStop, 5000) == win32event.WAIT_OBJECT_0:
                break

        if not self.stop_requested.is_set():
            # Worker died on its own: exit without reporting SERVICE_STOPPED so the
            # SCM treats it as a crash and applies the configured restart actions.
            self.logger.critical("Scheduler stopped unexpectedly. Exiting so Windows can restart the service.")
            logging.shutdown()
            os._exit(1)

        worker.join(timeout=30)
        self.logger.info("Service stopped.")
        servicemanager.LogMsg(
            servicemanager.EVENTLOG_INFORMATION_TYPE,
            servicemanager.PYS_SERVICE_STOPPED,
            (self._svc_name_, ""),
        )

    def _run_scheduler(self):
        try:
            from scheduler import Scheduler
            config = load_config(os.path.join(get_base_path(), "config.yaml"))
            Scheduler(config).start_service_mode(stop_event=self.stop_requested)
        except BaseException as e:
            # load_config calls sys.exit on bad config, so catch SystemExit too
            self.logger.critical(f"Scheduler worker failed: {e!r}", exc_info=True)


if __name__ == "__main__":
    if len(sys.argv) == 1 and getattr(sys, "frozen", False):
        # Launched by the SCM as a frozen executable
        servicemanager.Initialize()
        servicemanager.PrepareToHostSingle(ReportGeneratorService)
        servicemanager.StartServiceCtrlDispatcher()
    else:
        win32serviceutil.HandleCommandLine(ReportGeneratorService)
