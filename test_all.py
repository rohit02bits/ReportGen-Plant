import unittest
import os
import yaml
import shutil
import sqlite3
import pandas as pd
from datetime import datetime, date, timedelta
from db_client import DatabaseClient
from opc_client import OPCClient
from opc_collector import OPCCollector
from report_generator import ReportGenerator
from scheduler import Scheduler
from mock_db_setup import generate_batches_for_day, generate_single_batch, setup_and_populate_db

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
            
        # Create fresh SQLite test db schema using DatabaseClient.init_schema()
        db = DatabaseClient(cls.config)
        db.use_sqlite = True
        db.init_schema()

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

    def test_schema_sql_compatibility(self):
        """Test that schema.sql is 100% valid SQLite SQL and executes cleanly with no syntax errors."""
        temp_db_path = "temp_schema_test.db"
        if os.path.exists(temp_db_path):
            os.remove(temp_db_path)
            
        conn = sqlite3.connect(temp_db_path)
        with open("schema.sql", "r", encoding="utf-8") as f:
            sql_script = f.read()
            
        # Executes entire script
        conn.executescript(sql_script)
        
        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(*) FROM batching_data")
        count = cursor.fetchone()[0]
        self.assertGreater(count, 0)
        
        conn.close()
        if os.path.exists(temp_db_path):
            os.remove(temp_db_path)

    def test_database_client_insert_and_query(self):
        db = DatabaseClient(self.config)
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
        self.assertTrue((df["batch_no"] == 999).any())

    def test_database_bulk_insert_and_stats(self):
        db = DatabaseClient(self.config)
        db.use_sqlite = True
        
        mock_batches = generate_batches_for_day(date(2026, 5, 15), min_interval=10, max_interval=15)
        self.assertGreater(len(mock_batches), 50)
        
        inserted = db.insert_batching_data_bulk(mock_batches)
        self.assertEqual(inserted, len(mock_batches))
        
        stats = db.get_stats()
        self.assertGreaterEqual(stats["count"], len(mock_batches))
        self.assertIsNotNone(stats["min_time"])
        self.assertIsNotNone(stats["max_time"])

    def test_opc_client_simulation(self):
        client = OPCClient(self.config, force_simulation=True)
        client.connect()
        self.assertTrue(client.connected)
        self.assertTrue(client.simulation_mode)
        
        tags_to_read = list(self.config["opc"]["tag_mappings"].values())
        values = client.read_tags(tags_to_read)
        
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
