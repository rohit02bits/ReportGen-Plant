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
        
    def _build_conn_str(self, server):
        driver = self.db_config.get("driver", "ODBC Driver 17 for SQL Server")
        database = self.db_config.get("database", "PlantDB")
        trusted = self.db_config.get("trusted_connection", False)
        username = self.db_config.get("username", "")
        password = self.db_config.get("password", "")
        
        # Ensure TrustServerCertificate=yes is included to avoid SSL/TLS certificate errors
        extra_flags = "TrustServerCertificate=yes;"
        
        if trusted:
            return f"DRIVER={{{driver}}};SERVER={server};DATABASE={database};Trusted_Connection=yes;{extra_flags}"
        else:
            return f"DRIVER={{{driver}}};SERVER={server};DATABASE={database};UID={username};PWD={password};{extra_flags}"

    def _get_connection_string(self):
        server = self.db_config.get("server", "localhost")
        return self._build_conn_str(server)

    def _connect_sql_server(self):
        import pyodbc
        primary_server = self.db_config.get("server", "localhost")
        logger.info(f"Connecting to SQL Server: SERVER={primary_server}, DATABASE={self.db_config.get('database')}")
        
        # List of server name variations to try if primary fails with 08001 (instance not found)
        candidates = [primary_server]
        if "\\" in primary_server:
            # If "LOCALHOST\SQLEXPRESS", also try ".\SQLEXPRESS" and "(local)\SQLEXPRESS"
            inst_name = primary_server.split("\\", 1)[1]
            candidates.extend([f".\\{inst_name}", f"(local)\\{inst_name}", f"127.0.0.1\\{inst_name}"])
        elif primary_server.lower() in ["localhost", "127.0.0.1", ".", "(local)"]:
            candidates.extend([".", "(local)", "localhost", "127.0.0.1"])

        last_error = None
        for candidate in dict.fromkeys(candidates):
            conn_str = self._build_conn_str(candidate)
            try:
                logger.debug(f"Attempting SQL Server connection with SERVER={candidate}...")
                conn = pyodbc.connect(conn_str, timeout=3)
                if candidate != primary_server:
                    logger.info(f"Successfully connected to SQL Server using alternate server name: '{candidate}'")
                return conn
            except Exception as e:
                last_error = e
                logger.debug(f"Failed connecting to SQL Server with SERVER={candidate}: {e}")

        raise last_error

    def _connect_sqlite(self):
        logger.info(f"Connecting to SQLite database: {self.sqlite_path}")
        # Ensure parent directory exists
        sqlite_dir = os.path.dirname(os.path.abspath(self.sqlite_path))
        if sqlite_dir and not os.path.exists(sqlite_dir):
            os.makedirs(sqlite_dir, exist_ok=True)
            
        conn = sqlite3.connect(self.sqlite_path)
        # Ensure table exists
        cursor = conn.cursor()
        cursor.execute("""
        CREATE TABLE IF NOT EXISTS batching_data (
            batch_no INTEGER NOT NULL,
            batching_time TEXT NOT NULL,
            empty_val_1 REAL DEFAULT 0.00,
            wst_bqt_h_1 REAL DEFAULT 0.00,
            qtz REAL DEFAULT 0.00,
            wst_bqt_h_2 REAL DEFAULT 0.00,
            empty_val_2 REAL DEFAULT 0.00,
            an_coal REAL DEFAULT 0.00,
            wst_bqt_h_3 REAL DEFAULT 0.00,
            harfer_cok REAL DEFAULT 0.00,
            wst_bqt_l REAL DEFAULT 0.00,
            wst_fbl REAL DEFAULT 0.00
        );
        """)
        cursor.execute("CREATE INDEX IF NOT EXISTS IX_batching_data_batching_time ON batching_data (batching_time ASC);")
        conn.commit()
        return conn

    def get_connection(self):
        """
        Returns an active database connection (SQL Server or SQLite).
        Sets self.use_sqlite = True if falling back to SQLite.
        """
        if self.use_sqlite:
            return self._connect_sqlite()
        try:
            return self._connect_sql_server()
        except Exception as e:
            if self.fallback:
                logger.warning(f"SQL Server connection failed: {e}. Falling back to SQLite.")
                self.use_sqlite = True
                return self._connect_sqlite()
            raise e

    def init_schema(self):
        """
        Initializes the database schema by creating the `batching_data` table
        and index if they do not already exist. Works with both SQL Server and SQLite.
        """
        conn = self.get_connection()
        cursor = conn.cursor()
        try:
            if self.use_sqlite:
                logger.info("Ensuring SQLite schema is created...")
                cursor.execute("""
                CREATE TABLE IF NOT EXISTS batching_data (
                    batch_no INTEGER NOT NULL,
                    batching_time TEXT NOT NULL,
                    empty_val_1 REAL DEFAULT 0.00,
                    wst_bqt_h_1 REAL DEFAULT 0.00,
                    qtz REAL DEFAULT 0.00,
                    wst_bqt_h_2 REAL DEFAULT 0.00,
                    empty_val_2 REAL DEFAULT 0.00,
                    an_coal REAL DEFAULT 0.00,
                    wst_bqt_h_3 REAL DEFAULT 0.00,
                    harfer_cok REAL DEFAULT 0.00,
                    wst_bqt_l REAL DEFAULT 0.00,
                    wst_fbl REAL DEFAULT 0.00
                );
                """)
                cursor.execute("""
                CREATE INDEX IF NOT EXISTS IX_batching_data_batching_time 
                ON batching_data (batching_time ASC);
                """)
            else:
                logger.info("Ensuring SQL Server schema is created...")
                cursor.execute("""
                IF NOT EXISTS (SELECT * FROM sys.objects WHERE object_id = OBJECT_ID(N'[dbo].[batching_data]') AND type in (N'U'))
                BEGIN
                    CREATE TABLE [dbo].[batching_data](
                        [batch_no] [int] NOT NULL,
                        [batching_time] [datetime] NOT NULL,
                        [empty_val_1] [decimal](18, 2) NULL DEFAULT 0.00,
                        [wst_bqt_h_1] [decimal](18, 2) NULL DEFAULT 0.00,
                        [qtz] [decimal](18, 2) NULL DEFAULT 0.00,
                        [wst_bqt_h_2] [decimal](18, 2) NULL DEFAULT 0.00,
                        [empty_val_2] [decimal](18, 2) NULL DEFAULT 0.00,
                        [an_coal] [decimal](18, 2) NULL DEFAULT 0.00,
                        [wst_bqt_h_3] [decimal](18, 2) NULL DEFAULT 0.00,
                        [harfer_cok] [decimal](18, 2) NULL DEFAULT 0.00,
                        [wst_bqt_l] [decimal](18, 2) NULL DEFAULT 0.00,
                        [wst_fbl] [decimal](18, 2) NULL DEFAULT 0.00
                    ) ON [PRIMARY]
                END
                """)
                cursor.execute("""
                IF NOT EXISTS (SELECT * FROM sys.indexes WHERE name = N'IX_batching_data_batching_time' AND object_id = OBJECT_ID(N'[dbo].[batching_data]'))
                BEGIN
                    CREATE NONCLUSTERED INDEX [IX_batching_data_batching_time] 
                    ON [dbo].[batching_data] ([batching_time] ASC)
                    INCLUDE ([batch_no], [empty_val_1], [wst_bqt_h_1], [qtz], [wst_bqt_h_2], [empty_val_2], [an_coal], [wst_bqt_h_3], [harfer_cok], [wst_bqt_l], [wst_fbl])
                END
                """)
            conn.commit()
            logger.info("Database schema initialized successfully.")
            return True
        except Exception as e:
            logger.error(f"Failed to initialize database schema: {e}", exc_info=True)
            raise e
        finally:
            conn.close()

    def get_batching_data(self, start_dt: datetime, end_dt: datetime):
        """
        Queries the database for batching data between start_dt (inclusive) and end_dt (exclusive).
        Returns a pandas DataFrame.
        """
        columns_config = self.config.get("columns", [])
        
        db_fields = []
        for col in columns_config:
            field = col.get("db_field")
            if field and field not in db_fields:
                db_fields.append(field)
                
        if not db_fields:
            logger.error("No columns mapped in configuration columns list!")
            return pd.DataFrame()
            
        fields_str = ", ".join(db_fields)
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
            conn = self.get_connection()
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
        if "batching_time" not in data_dict or not data_dict["batching_time"]:
            data_dict["batching_time"] = datetime.now()
        
        if isinstance(data_dict["batching_time"], datetime):
            data_dict["batching_time"] = data_dict["batching_time"].strftime('%Y-%m-%d %H:%M:%S')

        schema_fields = [
            "batch_no", "batching_time", "empty_val_1", "wst_bqt_h_1", "qtz", 
            "wst_bqt_h_2", "empty_val_2", "an_coal", "wst_bqt_h_3", "harfer_cok", 
            "wst_bqt_l", "wst_fbl"
        ]
        
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
            conn = self.get_connection()
            table_name = "batching_data" if self.use_sqlite else "[dbo].[batching_data]"
            query = f"INSERT INTO {table_name} ({fields_str}) VALUES ({placeholders_str})"
            logger.info(f"Inserting batch {data_dict.get('batch_no')} into {table_name}")
            
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

    def insert_batching_data_bulk(self, records_list):
        """
        Inserts multiple batch records efficiently.
        Each item in records_list is a tuple/list matching the schema column order:
        (batch_no, batching_time, empty_val_1, wst_bqt_h_1, qtz, wst_bqt_h_2,
         empty_val_2, an_coal, wst_bqt_h_3, harfer_cok, wst_bqt_l, wst_fbl)
        """
        if not records_list:
            return 0

        conn = None
        try:
            conn = self.get_connection()
            table_name = "batching_data" if self.use_sqlite else "[dbo].[batching_data]"
            query = f"""
            INSERT INTO {table_name} (
                batch_no, batching_time, empty_val_1, wst_bqt_h_1, qtz, wst_bqt_h_2,
                empty_val_2, an_coal, wst_bqt_h_3, harfer_cok, wst_bqt_l, wst_fbl
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """
            cursor = conn.cursor()
            cursor.executemany(query, records_list)
            conn.commit()
            logger.info(f"Successfully bulk inserted {len(records_list)} records into {table_name}.")
            return len(records_list)
        except Exception as e:
            logger.error(f"Error bulk inserting records: {e}", exc_info=True)
            if conn:
                try:
                    conn.rollback()
                except Exception:
                    pass
            raise e
        finally:
            if conn:
                conn.close()

    def get_stats(self):
        """
        Returns basic statistics about the `batching_data` table.
        """
        conn = None
        try:
            conn = self.get_connection()
            table_name = "batching_data" if self.use_sqlite else "[dbo].[batching_data]"
            cursor = conn.cursor()
            cursor.execute(f"SELECT COUNT(*), MIN(batching_time), MAX(batching_time), MIN(batch_no), MAX(batch_no) FROM {table_name}")
            row = cursor.fetchone()
            if row:
                return {
                    "count": row[0],
                    "min_time": row[1],
                    "max_time": row[2],
                    "min_batch": row[3],
                    "max_batch": row[4],
                    "engine": "SQLite" if self.use_sqlite else "SQL Server"
                }
            return {"count": 0, "engine": "SQLite" if self.use_sqlite else "SQL Server"}
        except Exception as e:
            logger.warning(f"Could not retrieve table statistics: {e}")
            return {"error": str(e)}
        finally:
            if conn:
                conn.close()

    def clear_data(self, start_dt: datetime = None, end_dt: datetime = None):
        """
        Clears records from batching_data table, optionally bounded by start_dt and end_dt.
        """
        conn = None
        try:
            conn = self.get_connection()
            table_name = "batching_data" if self.use_sqlite else "[dbo].[batching_data]"
            cursor = conn.cursor()
            if start_dt and end_dt:
                query = f"DELETE FROM {table_name} WHERE batching_time >= ? AND batching_time < ?"
                cursor.execute(query, (start_dt.strftime('%Y-%m-%d %H:%M:%S'), end_dt.strftime('%Y-%m-%d %H:%M:%S')))
            elif start_dt:
                query = f"DELETE FROM {table_name} WHERE batching_time >= ?"
                cursor.execute(query, (start_dt.strftime('%Y-%m-%d %H:%M:%S'),))
            else:
                query = f"DELETE FROM {table_name}"
                cursor.execute(query)
            conn.commit()
            deleted_count = cursor.rowcount
            logger.info(f"Deleted records from {table_name}. Affected rows: {deleted_count}")
            return deleted_count
        except Exception as e:
            logger.error(f"Error clearing data from {table_name}: {e}", exc_info=True)
            raise e
        finally:
            if conn:
                conn.close()
