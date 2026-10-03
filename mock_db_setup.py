import os
import sys
import time
import random
import yaml
import argparse
from datetime import datetime, date, timedelta
from db_client import DatabaseClient

def load_config(config_path="config.yaml"):
    """Loads configuration file if available, otherwise returns default settings."""
    if os.path.exists(config_path):
        try:
            with open(config_path, "r", encoding="utf-8") as f:
                return yaml.safe_load(f)
        except Exception as e:
            print(f"Warning: Could not read {config_path}: {e}")
    return {
        "database": {
            "fallback_to_sqlite": True,
            "sqlite_db_path": "mock_plant.db"
        },
        "columns": []
    }

def generate_batches_for_day(target_date, min_interval=8, max_interval=15, start_batch_no=1):
    """
    Generates a full day's worth of batch data (00:00:00 to 23:59:59)
    with realistic plant parameter values.
    """
    current_time = datetime.combine(target_date, datetime.min.time())
    end_time = datetime.combine(target_date, datetime.max.time())
    
    batch_no = start_batch_no
    batches = []
    
    while current_time < end_time:
        interval_minutes = random.randint(min_interval, max_interval)
        interval_seconds = random.randint(0, 59)
        current_time += timedelta(minutes=interval_minutes, seconds=interval_seconds)
        
        if current_time >= end_time:
            break
            
        empty_val_1 = 0.00
        wst_bqt_h_1 = round(random.uniform(695.0, 706.0), 2)
        qtz = round(random.uniform(240.0, 280.0), 2)
        wst_bqt_h_2 = 0.00
        empty_val_2 = 0.00
        an_coal = 0.00
        wst_bqt_h_3 = round(random.uniform(1390.0, 1415.0), 2)
        
        # Harfer Coke occasionally spikes or drops, but usually around 290
        harfer_cok = round(random.choice([
            random.uniform(280.0, 295.0),
            random.uniform(280.0, 295.0),
            random.uniform(280.0, 295.0),
            random.uniform(130.0, 150.0),  # occasionally low
            random.uniform(400.0, 450.0)   # occasionally high
        ]), 2)
        
        wst_bqt_l = round(random.uniform(645.0, 660.0), 2)
        wst_fbl = round(random.uniform(245.0, 260.0), 2)

        batches.append((
            batch_no,
            current_time.strftime("%Y-%m-%d %H:%M:%S"),
            empty_val_1,
            wst_bqt_h_1,
            qtz,
            wst_bqt_h_2,
            empty_val_2,
            an_coal,
            wst_bqt_h_3,
            harfer_cok,
            wst_bqt_l,
            wst_fbl
        ))
        batch_no += 1
        
    return batches

def generate_single_batch(batch_no, timestamp=None):
    """Generates a single batch dictionary for live streaming or insertion."""
    if timestamp is None:
        timestamp = datetime.now()
    if isinstance(timestamp, datetime):
        timestamp_str = timestamp.strftime("%Y-%m-%d %H:%M:%S")
    else:
        timestamp_str = str(timestamp)

    return {
        "batch_no": int(batch_no),
        "batching_time": timestamp_str,
        "empty_val_1": 0.00,
        "wst_bqt_h_1": round(random.uniform(695.0, 706.0), 2),
        "qtz": round(random.uniform(240.0, 280.0), 2),
        "wst_bqt_h_2": 0.00,
        "empty_val_2": 0.00,
        "an_coal": 0.00,
        "wst_bqt_h_3": round(random.uniform(1390.0, 1415.0), 2),
        "harfer_cok": round(random.uniform(280.0, 295.0), 2),
        "wst_bqt_l": round(random.uniform(645.0, 660.0), 2),
        "wst_fbl": round(random.uniform(245.0, 260.0), 2)
    }

def setup_and_populate_db(config, target_engine="auto", days=7, specific_date=None, start_date=None, end_date=None, clear=False):
    """
    Creates tables and loads mock data into the configured or selected database.
    """
    db = DatabaseClient(config)
    
    if target_engine == "sqlite":
        db.use_sqlite = True
    elif target_engine == "sqlserver":
        db.use_sqlite = False
        db.fallback = False

    print(f"Connecting to database (Target mode: {target_engine})...")
    
    # 1. Initialize Tables
    print("1. Initializing schema and tables...")
    db.init_schema()
    print("   ✓ Table 'batching_data' and index created/verified.")

    # 2. Clear data if requested
    if clear:
        print("2. Clearing existing table records...")
        deleted = db.clear_data()
        print(f"   ✓ Removed {deleted} previous records.")

    # 3. Generate mock records
    print("3. Generating realistic plant batch data...")
    records = []
    
    today = datetime.now().date()
    
    if start_date and end_date:
        curr = start_date
        while curr <= end_date:
            print(f"   - Generating batches for {curr}...")
            records.extend(generate_batches_for_day(curr))
            curr += timedelta(days=1)
    else:
        for i in range(days):
            day = today - timedelta(days=i)
            print(f"   - Generating batches for past day: {day}...")
            records.extend(generate_batches_for_day(day))
            
        # Target specific date (e.g. 2026-05-15) if requested or if not in range
        if specific_date:
            spec_dt = datetime.strptime(specific_date, "%Y-%m-%d").date() if isinstance(specific_date, str) else specific_date
            print(f"   - Generating batches for target date: {spec_dt}...")
            records.extend(generate_batches_for_day(spec_dt))
        else:
            # Always ensure historical benchmark date 2026-05-15 exists for test reports
            benchmark_date = datetime.strptime("2026-05-15", "%Y-%m-%d").date()
            already_generated = any(datetime.strptime(r[1], "%Y-%m-%d %H:%M:%S").date() == benchmark_date for r in records)
            if not already_generated:
                print(f"   - Generating benchmark data for {benchmark_date}...")
                records.extend(generate_batches_for_day(benchmark_date))

    # 4. Insert into DB
    print(f"4. Inserting {len(records)} mock batch records into database...")
    inserted = db.insert_batching_data_bulk(records)
    print(f"   ✓ Successfully inserted {inserted} records.")

    # 5. Verify stats
    stats = db.get_stats()
    print("\nDatabase Status Summary:")
    print(f"  • Engine: {stats.get('engine')}")
    print(f"  • Total Records: {stats.get('count')}")
    print(f"  • Earliest Timestamp: {stats.get('min_time')}")
    print(f"  • Latest Timestamp: {stats.get('max_time')}")
    print(f"  • Batch Range: {stats.get('min_batch')} -> {stats.get('max_batch')}")

    return db

def stream_mock_data(config, interval_seconds=10, target_engine="auto"):
    """
    Streams continuous live mock data batches at the specified interval.
    Useful for testing continuous OPC collectors, scheduled report triggers, and real-time dashboards.
    """
    db = DatabaseClient(config)
    if target_engine == "sqlite":
        db.use_sqlite = True
    elif target_engine == "sqlserver":
        db.use_sqlite = False
        db.fallback = False

    db.init_schema()
    stats = db.get_stats()
    next_batch_no = (stats.get("max_batch") or 0) + 1

    print(f"\n[Live Stream] Streaming mock batches every {interval_seconds} seconds into {stats.get('engine')}...")
    print("Press Ctrl+C to stop.\n")

    try:
        while True:
            batch = generate_single_batch(next_batch_no)
            db.insert_batching_data(batch)
            print(f"[{batch['batching_time']}] Inserted Batch #{batch['batch_no']} | WST_BQT_H_1: {batch['wst_bqt_h_1']} | QTZ: {batch['qtz']} | HARFER_COK: {batch['harfer_cok']}")
            next_batch_no += 1
            time.sleep(interval_seconds)
    except KeyboardInterrupt:
        print("\n[Live Stream] Stopped live mock data streaming.")

def main():
    parser = argparse.ArgumentParser(
        description="Plant Database Schema Setup & Mock Data Processor",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Initialize tables and load 7 days of mock data into SQLite
  python mock_db_setup.py --sqlite

  # Initialize tables and load mock data into SQL Server (PlantDB)
  python mock_db_setup.py --sqlserver

  # Initialize tables only without loading data
  python mock_db_setup.py --init-only

  # Load mock data for a specific date (e.g. 2026-05-15)
  python mock_db_setup.py --date 2026-05-15

  # Clear existing records and reload 14 days of data
  python mock_db_setup.py --clear --days 14

  # Stream live mock batch data into the database every 5 seconds
  python mock_db_setup.py --stream --interval 5

  # Check database row counts and stats
  python mock_db_setup.py --stats
"""
    )
    
    parser.add_argument("--config", default="config.yaml", help="Path to config.yaml (default: config.yaml)")
    parser.add_argument("--target", choices=["auto", "sqlite", "sqlserver"], default="auto", help="Database target engine (default: auto from config)")
    parser.add_argument("--sqlite", action="store_true", help="Force SQLite target database")
    parser.add_argument("--sqlserver", action="store_true", help="Force SQL Server target database")
    parser.add_argument("--days", type=int, default=7, help="Number of past days to generate data for (default: 7)")
    parser.add_argument("--date", help="Specific target date in YYYY-MM-DD format (e.g. 2026-05-15)")
    parser.add_argument("--start-date", help="Range start date in YYYY-MM-DD format")
    parser.add_argument("--end-date", help="Range end date in YYYY-MM-DD format")
    parser.add_argument("--clear", action="store_true", help="Clear existing batching_data records before populating")
    parser.add_argument("--init-only", action="store_true", help="Only create tables and indexes without inserting mock data")
    parser.add_argument("--stream", action="store_true", help="Continuously insert live mock batches at intervals")
    parser.add_argument("--interval", type=int, default=10, help="Interval in seconds for live stream mode (default: 10)")
    parser.add_argument("--stats", action="store_true", help="Display current table statistics and record counts")

    args = parser.parse_args()

    config = load_config(args.config)

    target_engine = args.target
    if args.sqlite:
        target_engine = "sqlite"
    elif args.sqlserver:
        target_engine = "sqlserver"

    if args.stats:
        db = DatabaseClient(config)
        if target_engine == "sqlite":
            db.use_sqlite = True
        elif target_engine == "sqlserver":
            db.use_sqlite = False
            db.fallback = False
        stats = db.get_stats()
        print("\nDatabase Status Summary:")
        for k, v in stats.items():
            print(f"  • {k}: {v}")
        return

    if args.init_only:
        db = DatabaseClient(config)
        if target_engine == "sqlite":
            db.use_sqlite = True
        elif target_engine == "sqlserver":
            db.use_sqlite = False
            db.fallback = False
        db.init_schema()
        print("✓ Tables and indexes initialized successfully.")
        return

    if args.stream:
        stream_mock_data(config, interval_seconds=args.interval, target_engine=target_engine)
        return

    start_d = datetime.strptime(args.start_date, "%Y-%m-%d").date() if args.start_date else None
    end_d = datetime.strptime(args.end_date, "%Y-%m-%d").date() if args.end_date else None

    setup_and_populate_db(
        config=config,
        target_engine=target_engine,
        days=args.days,
        specific_date=args.date,
        start_date=start_d,
        end_date=end_d,
        clear=args.clear
    )

if __name__ == "__main__":
    main()
