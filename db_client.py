import os
import sys
import logging
import sqlite3
import pandas as pd
from datetime import datetime

# Setup logger
logger = logging.getLogger("ReportGenerator.DbClient")

class DatabaseClient:
    def __init__(self, config):
        self.config = config
        self.db_config = config.get("database", {})
        self.fallback = self.db_config.get("fallback_to_sqlite", True)
        sqlite_raw = self.db_config.get("sqlite_db_path", "mock_plant.db")
        if not os.path.isabs(sqlite_raw):
            base_dir = os.path.dirname(sys.executable) if getattr(sys, 'frozen', False) else os.path.dirname(os.path.abspath(__file__))
            self.sqlite_path = os.path.join(base_dir, sqlite_raw)
        else:
            self.sqlite_path = sqlite_raw
        self.use_sqlite = False
        
    def _get_connection_string(self):
        driver = self.db_config.get("driver", "ODBC Driver 17 for SQL Server")
        server = self.db_config.get("server", "localhost")
        database = self.db_config.get("database", "PlantDB")
        trusted = self.db_config.get("trusted_connection", False)
        username = self.db_config.get("username", "")
        password = self.db_config.get("password", "")
        
        if trusted:
            return f"DRIVER={{{driver}}};SERVER={server};DATABASE={database};Trusted_Connection=yes;"
        else:
            return f"DRIVER={{{driver}}};SERVER={server};DATABASE={database};UID={username};PWD={password};"

    def _connect_sql_server(self):
        import pyodbc
        conn_str = self._get_connection_string()
        logger.info(f"Connecting to SQL Server: SERVER={self.db_config.get('server')}, DATABASE={self.db_config.get('database')}")
        # Add timeout to connection
        return pyodbc.connect(conn_str, timeout=5)

    def _connect_sqlite(self):
        logger.info(f"Connecting to fallback SQLite database: {self.sqlite_path}")
        if not os.path.exists(self.sqlite_path):
            logger.warning(f"SQLite file '{self.sqlite_path}' does not exist. A mock database setup is recommended.")
        return sqlite3.connect(self.sqlite_path)

    def get_batching_data(self, start_dt: datetime, end_dt: datetime):
        """
        Queries the database for batching data between start_dt (inclusive) and end_dt (exclusive).
        Returns a pandas DataFrame.
        """
        # Determine fields to select from config
        columns_config = self.config.get("columns", [])
        
        # Ensure we have a set of unique db fields to query
        db_fields = []
        for col in columns_config:
            field = col.get("db_field")
            if field and field not in db_fields:
                db_fields.append(field)
                
        if not db_fields:
            logger.error("No columns mapped in configuration columns list!")
            return pd.DataFrame()
            
        fields_str = ", ".join(db_fields)
        # Note: If batching_time is mapped, it is already in db_fields. We need it for sorting/filtering.
        # Let's find the timestamp column in db_fields, or default to batching_time
        timestamp_col = "batching_time"
        for col in columns_config:
            if col.get("type") == "datetime":
                timestamp_col = col.get("db_field", "batching_time")
                break

        query = f"SELECT {fields_str} FROM batching_data WHERE {timestamp_col} >= ? AND {timestamp_col} < ? ORDER BY {timestamp_col} ASC"
        
        logger.info(f"Querying data from {start_dt} to {end_dt}")
        logger.debug(f"SQL: {query}")

        conn = None
        try:
            if self.use_sqlite:
                conn = self._connect_sqlite()
            else:
                try:
                    conn = self._connect_sql_server()
                except Exception as e:
                    if self.fallback:
                        logger.warning(f"SQL Server connection failed: {e}. Falling back to SQLite.")
                        self.use_sqlite = True
                        conn = self._connect_sqlite()
                    else:
                        raise e
            
            # Read sql query into DataFrame
            df = pd.read_sql_query(query, conn, params=(start_dt.strftime('%Y-%m-%d %H:%M:%S'), end_dt.strftime('%Y-%m-%d %H:%M:%S')))
            logger.info(f"Successfully retrieved {len(df)} records from the database.")
            return df
            
        except Exception as e:
            logger.error(f"Error querying database: {e}", exc_info=True)
            raise e
        finally:
            if conn:
                conn.close()
                logger.debug("Database connection closed.")

    def insert_batching_data(self, data_dict):
        """
        Inserts a single batching record into SQL Server or SQLite.
        data_dict contains keys corresponding to the database column names.
        """
        # Ensure we have a timestamp. If not, generate now.
        if "batching_time" not in data_dict or not data_dict["batching_time"]:
            data_dict["batching_time"] = datetime.now()
        
        # If batching_time is a datetime object, format it for insertion
        if isinstance(data_dict["batching_time"], datetime):
            data_dict["batching_time"] = data_dict["batching_time"].strftime('%Y-%m-%d %H:%M:%S')

        # Clean/sanitize data_dict fields to match table schema
        schema_fields = [
            "batch_no", "batching_time", "empty_val_1", "wst_bqt_h_1", "qtz", 
            "wst_bqt_h_2", "empty_val_2", "an_coal", "wst_bqt_h_3", "harfer_cok", 
            "wst_bqt_l", "wst_fbl"
        ]
        
        # Build query fields and placeholders
        fields = []
        placeholders = []
        values = []
        for field in schema_fields:
            if field in data_dict:
                fields.append(field)
                placeholders.append("?")
                val = data_dict[field]
                if field == "batch_no":
                    val = int(val)
                elif field != "batching_time" and val is not None:
                    val = float(val)
                values.append(val)
        
        fields_str = ", ".join(fields)
        placeholders_str = ", ".join(placeholders)
        
        conn = None
        try:
            if self.use_sqlite:
                conn = self._connect_sqlite()
                table_name = "batching_data"
            else:
                try:
                    conn = self._connect_sql_server()
                    table_name = "[dbo].[batching_data]"
                except Exception as e:
                    if self.fallback:
                        logger.warning(f"SQL Server connection failed during write: {e}. Falling back to SQLite.")
                        self.use_sqlite = True
                        conn = self._connect_sqlite()
                        table_name = "batching_data"
                    else:
                        raise e

            query = f"INSERT INTO {table_name} ({fields_str}) VALUES ({placeholders_str})"
            logger.info(f"Inserting batch {data_dict.get('batch_no')} into {table_name}")
            logger.debug(f"SQL: {query} with values {values}")
            
            cursor = conn.cursor()
            cursor.execute(query, values)
            conn.commit()
            logger.info("Successfully inserted record into database.")
            return True
            
        except Exception as e:
            logger.error(f"Error inserting into database: {e}", exc_info=True)
            if conn:
                try:
                    conn.rollback()
                except Exception:
                    pass
            raise e
        finally:
            if conn:
                conn.close()
                logger.debug("Database connection closed.")

