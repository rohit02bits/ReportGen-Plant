import unittest
import os
import yaml
import shutil
import pandas as pd
from datetime import datetime, date, timedelta
from db_client import DatabaseClient
from opc_client import OPCClient
from opc_collector import OPCCollector
from report_generator import ReportGenerator
from scheduler import Scheduler

class TestPlantBatchingSystem(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # Load configuration
        with open("config.yaml", "r", encoding="utf-8") as f:
            cls.config = yaml.safe_load(f)
            
        # Ensure fallback_to_sqlite is true and use a test db file
        cls.config["database"]["sqlite_db_path"] = "test_plant.db"
        cls.config["database"]["fallback_to_sqlite"] = True
        
        # Override output dir for testing reports
        cls.config["reporting"]["output_dir"] = "./test_reports"
        
        # Remove existing test db and test reports if they exist
        if os.path.exists("test_plant.db"):
            try:
                os.remove("test_plant.db")
            except Exception:
                pass
        if os.path.exists("./test_reports"):
            try:
                shutil.rmtree("./test_reports")
            except Exception:
                pass
            
        # Create fresh SQLite test db
        import sqlite3
        conn = sqlite3.connect("test_plant.db")
        cursor = conn.cursor()
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
        conn.close()

    @classmethod
    def tearDownClass(cls):
        # Cleanup test db and test reports
        if os.path.exists("test_plant.db"):
            try:
                os.remove("test_plant.db")
            except Exception:
                pass
        if os.path.exists("./test_reports"):
            try:
                shutil.rmtree("./test_reports")
            except Exception:
                pass

    def test_database_client_insert_and_query(self):
        db = DatabaseClient(self.config)
        # Ensure we are using SQLite
        db.use_sqlite = True
        
        # Create test record
        test_data = {
            "batch_no": 999,
            "batching_time": datetime.now(),
            "empty_val_1": 1.1,
            "wst_bqt_h_1": 2.2,
            "qtz": 3.3,
            "wst_bqt_h_2": 4.4,
            "empty_val_2": 5.5,
            "an_coal": 6.6,
            "wst_bqt_h_3": 7.7,
            "harfer_cok": 8.8,
            "wst_bqt_l": 9.9,
            "wst_fbl": 10.1
        }
        
        # Test Insertion
        success = db.insert_batching_data(test_data)
        self.assertTrue(success)
        
        # Test Query
        start_dt = datetime.now() - timedelta(minutes=5)
        end_dt = datetime.now() + timedelta(minutes=5)
        df = db.get_batching_data(start_dt, end_dt)
        
        self.assertFalse(df.empty)
        self.assertEqual(len(df), 1)
        self.assertEqual(int(df.iloc[0]["batch_no"]), 999)
        self.assertEqual(float(df.iloc[0]["qtz"]), 3.3)

    def test_opc_client_simulation(self):
        # Force simulation
        client = OPCClient(self.config, force_simulation=True)
        client.connect()
        self.assertTrue(client.connected)
        self.assertTrue(client.simulation_mode)
        
        # Read mock tags
        tags_to_read = list(self.config["opc"]["tag_mappings"].values())
        values = client.read_tags(tags_to_read)
        
        # Verify tag counts and values
        self.assertEqual(len(values), len(tags_to_read))
        batch_no_tag = self.config["opc"]["tag_mappings"]["batch_no"]
        self.assertIsNotNone(values[batch_no_tag])
        
        client.disconnect()
        self.assertFalse(client.connected)

    def test_report_generator(self):
        db = DatabaseClient(self.config)
        db.use_sqlite = True
        
        # Insert a record
        db.insert_batching_data({
            "batch_no": 1001,
            "batching_time": datetime.now(),
            "empty_val_1": 0.0,
            "wst_bqt_h_1": 700.0,
            "qtz": 250.0,
            "wst_bqt_h_2": 0.0,
            "empty_val_2": 0.0,
            "an_coal": 0.0,
            "wst_bqt_h_3": 1400.0,
            "harfer_cok": 290.0,
            "wst_bqt_l": 650.0,
            "wst_fbl": 250.0
        })
        
        # Fetch the data
        start_dt = datetime.now() - timedelta(minutes=5)
        end_dt = datetime.now() + timedelta(minutes=5)
        df = db.get_batching_data(start_dt, end_dt)
        
        # Generate report
        rep = ReportGenerator(self.config)
        filepath = rep.generate_excel_report("Test Shift", start_dt, end_dt, df)
        
        self.assertTrue(os.path.exists(filepath))
        self.assertTrue(filepath.endswith(".xlsx"))

if __name__ == "__main__":
    unittest.main()
