import os
import sys
import yaml
import argparse
import logging
from datetime import datetime, date
from scheduler import Scheduler

def get_base_path():
    """
    Returns the base directory of the application.
    If packaged with PyInstaller, this will be the directory containing the executable.
    """
    if getattr(sys, 'frozen', False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))

def setup_logging(base_dir, log_level=logging.INFO):
    """
    Configures logging to both console and a rotating log file.
    The log file rolls over after 50 MB (keeps 5 backup logs).
    """
    from logging.handlers import RotatingFileHandler
    log_dir = os.path.join(base_dir, "logs")
    # Create logs directory if it doesn't exist
    os.makedirs(log_dir, exist_ok=True)
    
    log_format = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
    date_format = "%Y-%m-%d %H:%M:%S"
    
    # Configure root logger
    logging.basicConfig(
        level=log_level,
        format=log_format,
        datefmt=date_format,
        handlers=[
            logging.StreamHandler(sys.stdout),
            RotatingFileHandler(
                os.path.join(log_dir, "reporter.log"),
                maxBytes=50 * 1024 * 1024,
                backupCount=5,
                encoding="utf-8"
            )
        ]
    )

def load_config(config_path):
    """
    Loads configuration from a YAML file.
    """
    if not os.path.exists(config_path):
        print(f"Error: Configuration file '{config_path}' not found.", file=sys.stderr)
        sys.exit(1)
        
    try:
        with open(config_path, "r", encoding="utf-8") as f:
            return yaml.safe_load(f)
    except Exception as e:
        print(f"Error parsing configuration file '{config_path}': {e}", file=sys.stderr)
        sys.exit(1)

def main():
    parser = argparse.ArgumentParser(
        description="SQL Server Excel Report Generator for Plant Batching Data",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Run Shift A report for today
  python main.py --run-shift "Shift A"
  
  # Run Shift A report for a specific historical date
  python main.py --run-shift "Shift A" --date 2026-05-15
  
  # Run whichever report is currently due (e.g. for Windows Task Scheduler)
  python main.py --run-current
  
  # Start the background service daemon
  python main.py --service
"""
    )
    
    parser.add_argument("--config", default="config.yaml", help="Path to config.yaml file (default: config.yaml)")
    parser.add_argument("--run-shift", help="Generate report for a specific shift immediately (e.g., 'Shift A', 'Shift B', 'Shift C', 'Daily')")
    parser.add_argument("--run-current", action="store_true", help="Auto-detect and run the shift report that is currently due")
    parser.add_argument("--date", help="Target date for the report in YYYY-MM-DD format (defaults to current date)")
    parser.add_argument("--service", action="store_true", help="Start the continuous background scheduler service")
    parser.add_argument("--dry-run", action="store_true", help="Use with --service to test scheduler setup without waiting")
    parser.add_argument("--collect-opc", action="store_true", help="Start the continuous OPC DA data collection service")
    parser.add_argument("--opc-sim", action="store_true", help="Force OPC DA client into simulation/mock mode")
    parser.add_argument("--init-db", action="store_true", help="Initialize database schema, tables, and indexes")
    parser.add_argument("--mock-data", action="store_true", help="Generate and insert mock plant batch data into the connected database")
    parser.add_argument("--mock-days", type=int, default=7, help="Number of past days for mock data generation (default: 7)")
    parser.add_argument("--db-stats", action="store_true", help="Display record count and statistics for the connected database")
    parser.add_argument("--log-level", default="INFO", choices=["DEBUG", "INFO", "WARNING", "ERROR"], help="Set the logging detail level (default: INFO)")

    args = parser.parse_args()

    base_dir = get_base_path()

    # Initialize Logging
    numeric_level = getattr(logging, args.log_level.upper(), None)
    setup_logging(base_dir, numeric_level)
    logger = logging.getLogger("ReportGenerator.Main")
    
    logger.info("Application starting...")
    
    # Resolve config path
    config_path = args.config
    if not os.path.isabs(config_path):
        config_path = os.path.join(base_dir, config_path)
        
    logger.info(f"Loading configuration from: {config_path}")
    config = load_config(config_path)
    
    # Parse target date if provided
    target_date = date.today()
    if args.date:
        try:
            target_date = datetime.strptime(args.date, "%Y-%m-%d").date()
            logger.info(f"Target date overridden to: {target_date}")
        except ValueError:
            logger.error(f"Invalid date format: '{args.date}'. Must be YYYY-MM-DD.")
            sys.exit(1)

    # Initialize Scheduler/Executor
    scheduler = Scheduler(config)

    # Route execution based on CLI args
    if args.init_db:
        from db_client import DatabaseClient
        db = DatabaseClient(config)
        logger.info("Initializing database tables and schema...")
        db.init_schema()
        logger.info("Database tables initialized successfully.")

    elif args.mock_data:
        from mock_db_setup import setup_and_populate_db
        logger.info(f"Generating mock batch records for {args.mock_days} days...")
        setup_and_populate_db(config, target_engine="auto", days=args.mock_days, specific_date=args.date)
        logger.info("Mock data generated and inserted successfully.")

    elif args.db_stats:
        from db_client import DatabaseClient
        db = DatabaseClient(config)
        stats = db.get_stats()
        print("\nDatabase Status Summary:")
        for k, v in stats.items():
            print(f"  • {k}: {v}")

    elif args.run_shift:
        try:
            scheduler.run_shift_report(args.run_shift, target_date)
            logger.info("Report execution completed successfully.")
        except Exception as e:
            logger.critical(f"Report execution failed: {e}")
            sys.exit(1)
            
    elif args.run_current:
        logger.info("Identifying currently due report...")
        due_shift, run_date = scheduler.find_current_due_shift()
        
        if due_shift:
            logger.info(f"Due report identified: '{due_shift['name']}' for run date {run_date}")
            try:
                scheduler.run_shift_report(due_shift["name"], run_date)
                logger.info("Due report execution completed successfully.")
            except Exception as e:
                logger.critical(f"Due report execution failed: {e}")
                sys.exit(1)
        else:
            logger.warning("No scheduled shifts were found to be due.")
            
    elif args.service:
        scheduler.start_service_mode(dry_run=args.dry_run)

    elif args.collect_opc:
        from opc_collector import OPCCollector
        logger.info("Starting OPC DA Data Collector...")
        collector = OPCCollector(config, force_simulation=args.opc_sim)
        try:
            collector.start()
        except KeyboardInterrupt:
            logger.info("OPC Collector service stopped.")
        except Exception as e:
            logger.critical(f"OPC Collector crashed: {e}")
            sys.exit(1)
        
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
