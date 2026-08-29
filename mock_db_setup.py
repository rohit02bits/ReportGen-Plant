import os
import sqlite3
import random
from datetime import datetime, timedelta

def create_mock_database(db_path="mock_plant.db"):
    """
    Creates an SQLite database and populates it with realistic plant batching data
    spanning the last 7 days, plus a specific target date (2026-05-15) for testing.
    """
    print(f"Creating mock database at: {os.path.abspath(db_path)}")
    
    # Remove existing database if it exists to start fresh
    if os.path.exists(db_path):
        try:
            os.remove(db_path)
            print("Removed existing mock database.")
        except Exception as e:
            print(f"Warning: Could not remove existing database: {e}")

    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()

    # Create the table
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS batching_data (
        batch_no INTEGER,
        batching_time TEXT,
        empty_val_1 REAL,
        wst_bqt_h_1 REAL,
        qtz REAL,
        wst_bqt_h_2 REAL,
        empty_val_2 REAL,
        an_coal REAL,
        wst_bqt_h_3 REAL,
        harfer_cok REAL,
        wst_bqt_l REAL,
        wst_fbl REAL
    )
    """)
    conn.commit()

    # Function to generate batches for a specific day
    def generate_batches_for_day(target_date):
        # We start batches from 00:00:00 to 23:59:59
        current_time = datetime.combine(target_date, datetime.min.time())
        end_time = datetime.combine(target_date, datetime.max.time())
        
        batch_no = 1
        batches = []
        
        while current_time < end_time:
            # Batch interval is between 8 to 15 minutes
            interval_minutes = random.randint(8, 15)
            interval_seconds = random.randint(0, 59)
            current_time += timedelta(minutes=interval_minutes, seconds=interval_seconds)
            
            if current_time >= end_time:
                break
                
            # Realistic data values mirroring the user screenshot:
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
                random.uniform(130.0, 150.0), # occasionally low
                random.uniform(400.0, 450.0)  # occasionally high
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

    # Generate data for the last 7 days
    today = datetime.now().date()
    all_data = []
    
    print("Generating batch records for the last 7 days...")
    for i in range(8):
        day = today - timedelta(days=i)
        all_data.extend(generate_batches_for_day(day))
        
    # Generate data specifically for the screenshot date 2026-05-15
    screenshot_date = datetime.strptime("2026-05-15", "%Y-%m-%d").date()
    # Check if we already generated it to avoid duplication
    days_diffs = [abs((today - timedelta(days=i)) - screenshot_date).days for i in range(8)]
    if min(days_diffs) > 0:
        print("Generating batch records for screenshot date 2026-05-15...")
        all_data.extend(generate_batches_for_day(screenshot_date))

    # Insert into database
    cursor.executemany("""
    INSERT INTO batching_data (
        batch_no, batching_time, empty_val_1, wst_bqt_h_1, qtz, wst_bqt_h_2,
        empty_val_2, an_coal, wst_bqt_h_3, harfer_cok, wst_bqt_l, wst_fbl
    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, all_data)
    
    conn.commit()
    
    # Query count to verify
    cursor.execute("SELECT COUNT(*) FROM batching_data")
    count = cursor.fetchone()[0]
    print(f"Successfully populated database with {count} mock records.")
    
    conn.close()

if __name__ == "__main__":
    create_mock_database()
