import time
import logging
import schedule
from datetime import datetime, date, timedelta
from db_client import DatabaseClient
from report_generator import ReportGenerator

logger = logging.getLogger("ReportGenerator.Scheduler")

class Scheduler:
    def __init__(self, config):
        self.config = config
        self.db_client = DatabaseClient(config)
        self.report_gen = ReportGenerator(config)
        self.shifts = config.get("shifts", [])

    def calculate_shift_window(self, shift_def, run_date: date):
        """
        Calculates the start and end datetime for a shift based on the run_date.
        """
        start_time_str = shift_def.get("start_time", "00:00:00")
        end_time_str = shift_def.get("end_time", "00:00:00")
        start_offset = shift_def.get("start_day_offset", 0)
        end_offset = shift_def.get("end_day_offset", 0)

        # Parse times
        start_t = datetime.strptime(start_time_str, "%H:%M:%S").time()
        end_t = datetime.strptime(end_time_str, "%H:%M:%S").time()

        # Apply date offsets
        start_d = run_date + timedelta(days=start_offset)
        end_d = run_date + timedelta(days=end_offset)

        # Combine to datetime objects
        start_dt = datetime.combine(start_d, start_t)
        end_dt = datetime.combine(end_d, end_t)

        return start_dt, end_dt

    def run_shift_report(self, shift_name: str, run_date: date = None):
        """
        Runs the query and generates the report for a specific shift by name.
        """
        if run_date is None:
            run_date = date.today()

        # Find shift definition
        shift_def = None
        for s in self.shifts:
            if s["name"].lower() == shift_name.lower():
                shift_def = s
                break

        if not shift_def:
            raise ValueError(f"Shift '{shift_name}' is not defined in configuration.")

        logger.info(f"Triggering report generation for '{shift_def['name']}' with run date: {run_date}")
        
        # Calculate coverage window
        start_dt, end_dt = self.calculate_shift_window(shift_def, run_date)
        logger.info(f"Report coverage window: {start_dt} to {end_dt}")

        try:
            # Query data
            df = self.db_client.get_batching_data(start_dt, end_dt)
            
            # Generate excel
            filepath = self.report_gen.generate_excel_report(shift_def["name"], start_dt, end_dt, df)
            logger.info(f"Successfully generated report for '{shift_def['name']}': {filepath}")
            return filepath
        except Exception as e:
            logger.error(f"Failed to generate report for shift '{shift_name}': {e}", exc_info=True)
            raise e

    def find_current_due_shift(self, current_time: datetime = None):
        """
        Identifies which shift's report is due right now.
        Returns the shift definition, the calculated run date, and the time difference.
        This is based on identifying the shift whose run_time is closest in the past.
        """
        if current_time is None:
            current_time = datetime.now()

        current_date = current_time.date()
        due_shifts = []

        for s in self.shifts:
            run_time_str = s.get("run_time", "00:00")
            run_t = datetime.strptime(run_time_str, "%H:%M").time()
            
            # Today's scheduled run datetime
            today_run_dt = datetime.combine(current_date, run_t)
            
            # Yesterday's scheduled run datetime
            yesterday_run_dt = datetime.combine(current_date - timedelta(days=1), run_t)
            
            # Tomorrow's scheduled run datetime (for edge cases)
            tomorrow_run_dt = datetime.combine(current_date + timedelta(days=1), run_t)
            
            # Find the closest past scheduled runs
            for run_dt, run_d in [(yesterday_run_dt, current_date - timedelta(days=1)),
                                  (today_run_dt, current_date),
                                  (tomorrow_run_dt, current_date + timedelta(days=1))]:
                if run_dt <= current_time:
                    diff = current_time - run_dt
                    due_shifts.append((diff, s, run_d, run_dt))

        if not due_shifts:
            return None, None

        # Sort by smallest time difference (meaning it ran most recently in the past)
        due_shifts.sort(key=lambda x: x[0])
        best_diff, best_shift, best_run_date, best_run_dt = due_shifts[0]
        
        logger.info(f"Closest scheduled run in the past was '{best_shift['name']}' at {best_run_dt} (diff: {best_diff})")
        return best_shift, best_run_date

    def start_service_mode(self, dry_run: bool = False):
        """
        Starts the continuous service loop scheduling tasks at the defined run times.
        """
        logger.info("Starting background scheduler service mode...")
        
        try:
            # Clear any existing jobs
            schedule.clear()

            # Define job wrapper
            def job_wrapper(shift):
                logger.info(f"Scheduled job triggered for {shift['name']}")
                try:
                    # In continuous mode, the run date is today's date
                    filepath = self.run_shift_report(shift["name"], date.today())
                    logger.info(f"Scheduled job completed successfully for {shift['name']}. Generated report: {filepath}")
                except Exception as e:
                    logger.error(f"Error executing scheduled job for {shift['name']}: {e}", exc_info=True)

            # Register jobs
            for s in self.shifts:
                run_time_str = s.get("run_time", "00:00")
                logger.info(f"Scheduling '{s['name']}' to run daily at {run_time_str}")
                if not dry_run:
                    schedule.every().day.at(run_time_str).do(job_wrapper, shift=s)
                else:
                    logger.info(f"[Dry Run] Registered '{s['name']}' schedule daily at {run_time_str}")

            if dry_run:
                logger.info("Dry run completed. Scheduler verified successfully.")
                return

            logger.info("Scheduler service is running. Press Ctrl+C to exit.")
            while True:
                schedule.run_pending()
                time.sleep(1)
        except KeyboardInterrupt:
            logger.info("Scheduler service stopped by user (KeyboardInterrupt).")
        except Exception as e:
            logger.critical(f"Scheduler service crashed with unexpected error: {e}", exc_info=True)
            raise e
        finally:
            logger.info("Scheduler service has ended.")
