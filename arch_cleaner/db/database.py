import sqlite3
import json
import time
import logging
from pathlib import Path
from typing import List, Optional, Tuple, Any, Dict

# Assuming models are defined in core.models
# Adjust import path if structure changes
from ..core.models import ScannedItem, PackageInfo, DuplicateSet, ActionFeedback

logger = logging.getLogger(__name__)

class DatabaseManager:
    """Handles all interactions with the SQLite database."""

    def __init__(self, db_path: Path):
        self.db_path = db_path
        self.conn: Optional[sqlite3.Connection] = None
        self._connect()
        self._create_tables()

    def _connect(self):
        """Establishes a connection to the SQLite database."""
        try:
            # Ensure the directory exists
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
            # `check_same_thread=False` might be needed if accessed by different threads,
            # but requires careful handling of transactions. Start with True.
            self.conn = sqlite3.connect(self.db_path, detect_types=sqlite3.PARSE_DECLTYPES | sqlite3.PARSE_COLNAMES)
            self.conn.row_factory = sqlite3.Row # Access columns by name
            self.conn.execute("PRAGMA foreign_keys = ON;")
            logger.info(f"Connected to database: {self.db_path}")
        except sqlite3.Error as e:
            logger.error(f"Error connecting to database {self.db_path}: {e}", exc_info=True)
            self.conn = None # Ensure conn is None if connection failed
            raise # Re-raise the exception to signal failure

    def _create_tables(self):
        """Creates necessary database tables if they don't exist."""
        if not self.conn:
            logger.error("Cannot create tables, no database connection.")
            return

        # Use TEXT for paths as Path objects are not directly storable
        # Use REAL for timestamps (Unix epoch float)
        # Use TEXT for JSON storage of complex fields like lists/dicts
        sql_statements = [
            """
            CREATE TABLE IF NOT EXISTS scan_history (
                scan_id INTEGER PRIMARY KEY AUTOINCREMENT,
                start_time REAL NOT NULL,
                end_time REAL,
                duration_seconds REAL,
                items_found INTEGER,
                errors TEXT
            );
            """,
            """
            CREATE TABLE IF NOT EXISTS scanned_items (
                path TEXT PRIMARY KEY,
                scan_id INTEGER,
                size_bytes INTEGER NOT NULL,
                last_accessed REAL NOT NULL,
                last_modified REAL NOT NULL,
                item_type TEXT NOT NULL, -- 'file', 'directory', 'cache', 'log' etc.
                file_hash TEXT, -- SHA256 hash for duplicate detection
                is_duplicate BOOLEAN DEFAULT FALSE,
                extra_info TEXT, -- JSON dictionary for additional data
                FOREIGN KEY (scan_id) REFERENCES scan_history(scan_id) ON DELETE CASCADE
            );
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_scanned_items_type ON scanned_items(item_type);
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_scanned_items_size ON scanned_items(size_bytes);
            """,
             """
            CREATE INDEX IF NOT EXISTS idx_scanned_items_hash ON scanned_items(file_hash);
            """,
            """
            CREATE TABLE IF NOT EXISTS packages (
                name TEXT PRIMARY KEY,
                scan_id INTEGER,
                version TEXT NOT NULL,
                size_bytes INTEGER NOT NULL,
                description TEXT,
                install_date REAL,
                last_used REAL, -- Heuristic
                is_orphan BOOLEAN DEFAULT FALSE,
                is_dependency BOOLEAN DEFAULT FALSE,
                required_by TEXT, -- JSON list
                optional_for TEXT, -- JSON list
                FOREIGN KEY (scan_id) REFERENCES scan_history(scan_id) ON DELETE CASCADE
            );
            """,
             """
            CREATE INDEX IF NOT EXISTS idx_packages_orphan ON packages(is_orphan);
            """,
            # Removed duplicate_sets and duplicate_files tables, will mark items in scanned_items directly
            """
            CREATE TABLE IF NOT EXISTS action_feedback (
                feedback_id INTEGER PRIMARY KEY AUTOINCREMENT,
                suggestion_id TEXT NOT NULL, -- Corresponds to Suggestion.id
                suggestion_type TEXT NOT NULL,
                item_details TEXT NOT NULL, -- e.g., file path, package name
                action_taken TEXT NOT NULL, -- 'APPROVED', 'REJECTED', 'SKIPPED'
                timestamp REAL NOT NULL,
                user_comment TEXT
            );
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_feedback_suggestion ON action_feedback(suggestion_id);
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_feedback_type ON action_feedback(suggestion_type);
            """
            # Removed current_suggestions table
        ]
        try:
            with self.conn: # Context manager handles commit/rollback
                cursor = self.conn.cursor()
                for statement in sql_statements:
                    cursor.execute(statement)
                logger.info("Database tables created or verified successfully.")
        except sqlite3.Error as e:
            logger.error(f"Error creating database tables: {e}", exc_info=True)
            # Consider closing connection or handling more gracefully
            raise

    def execute_sql(self, sql: str, params: tuple = ()) -> Optional[sqlite3.Cursor]:
        """Executes arbitrary SQL with parameters."""
        if not self.conn:
            logger.error("Cannot execute SQL, no database connection.")
            return None
        try:
            cursor = self.conn.cursor()
            cursor.execute(sql, params)
            return cursor
        except sqlite3.Error as e:
            logger.error(f"Error executing SQL: {sql} with params {params} - {e}", exc_info=True)
            return None # Or re-raise

    def execute_script(self, sql_script: str) -> bool:
        """Executes a potentially multi-statement SQL script."""
        if not self.conn:
            logger.error("Cannot execute script, no database connection.")
            return False
        try:
            with self.conn:
                self.conn.executescript(sql_script)
            return True
        except sqlite3.Error as e:
            logger.error(f"Error executing SQL script: {e}", exc_info=True)
            return False

    # --- Scan History Methods ---
    def start_scan(self) -> Optional[int]:
        """Records the start of a scan and returns the scan ID."""
        sql = "INSERT INTO scan_history (start_time) VALUES (?)"
        cursor = self.execute_sql(sql, (time.time(),))
        return cursor.lastrowid if cursor else None

    def end_scan(self, scan_id: int, items_found: int, errors: Optional[str] = None):
        """Updates the scan history record upon completion."""
        sql_get_start = "SELECT start_time FROM scan_history WHERE scan_id = ?"
        cursor_start = self.execute_sql(sql_get_start, (scan_id,))
        if not cursor_start: return
        row = cursor_start.fetchone()
        if not row: return

        start_time = row['start_time']
        end_time = time.time()
        duration = end_time - start_time if start_time else None

        sql_update = """
            UPDATE scan_history
            SET end_time = ?, duration_seconds = ?, items_found = ?, errors = ?
            WHERE scan_id = ?
        """
        self.execute_sql(sql_update, (end_time, duration, items_found, errors, scan_id))
        if self.conn: self.conn.commit() # Commit after update

    def get_last_scan_time(self) -> Optional[float]:
        """Gets the end time of the most recent successful scan."""
        sql = "SELECT MAX(end_time) FROM scan_history WHERE errors IS NULL"
        cursor = self.execute_sql(sql)
        if cursor:
            result = cursor.fetchone()
            return result[0] if result and result[0] else None
        return None

    # --- Scanned Item Methods ---
    def clear_scan_data(self, scan_id: int):
        """Removes data associated with a specific scan ID."""
        logger.warning(f"Clearing data for scan_id: {scan_id}")
        # Order matters due to foreign keys
        sqls = [
            "DELETE FROM scanned_items WHERE scan_id = ?",
            "DELETE FROM packages WHERE scan_id = ?",
            # Keep scan_history record but maybe mark as cleared? Or delete?
            # "DELETE FROM scan_history WHERE scan_id = ?"
        ]
        if not self.conn: return
        try:
            with self.conn:
                for sql in sqls:
                    self.execute_sql(sql, (scan_id,))
            logger.info(f"Successfully cleared data for scan_id: {scan_id}")
        except sqlite3.Error as e:
            logger.error(f"Error clearing scan data for scan_id {scan_id}: {e}", exc_info=True)


    def add_scanned_items_batch(self, items: List[ScannedItem], scan_id: int):
        """Adds a batch of scanned items to the database."""
        if not self.conn: return
        sql = """
            INSERT OR REPLACE INTO scanned_items
            (path, scan_id, size_bytes, last_accessed, last_modified, item_type, file_hash, is_duplicate, extra_info)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """
        data_tuples = [
            (
                str(item.path), scan_id, item.size_bytes, item.last_accessed, item.last_modified,
                item.item_type, item.extra_info.get('hash'), item.extra_info.get('is_duplicate', False),
                json.dumps(item.extra_info) if item.extra_info else None
            ) for item in items
        ]
        try:
            with self.conn:
                self.conn.executemany(sql, data_tuples)
            logger.debug(f"Added/Replaced {len(data_tuples)} scanned items for scan_id {scan_id}.")
        except sqlite3.Error as e:
            logger.error(f"Error adding scanned items batch: {e}", exc_info=True)

    def get_scanned_items(self, item_type: Optional[str] = None, min_size: Optional[int] = None) -> List[ScannedItem]:
        """Retrieves scanned items, optionally filtered."""
        if not self.conn: return []
        sql = "SELECT path, size_bytes, last_accessed, last_modified, item_type, extra_info FROM scanned_items"
        conditions = []
        params = []
        if item_type:
            conditions.append("item_type = ?")
            params.append(item_type)
        if min_size is not None:
            conditions.append("size_bytes >= ?")
            params.append(min_size)

        if conditions:
            sql += " WHERE " + " AND ".join(conditions)

        cursor = self.execute_sql(sql, tuple(params))
        items = []
        if cursor:
            for row in cursor.fetchall():
                extra_info = json.loads(row['extra_info']) if row['extra_info'] else {}
                items.append(ScannedItem(
                    path=Path(row['path']),
                    size_bytes=row['size_bytes'],
                    last_accessed=row['last_accessed'],
                    last_modified=row['last_modified'],
                    item_type=row['item_type'],
                    extra_info=extra_info
                ))
        return items

    def find_potential_duplicates(self, min_size: int) -> List[Tuple[str, int, int]]:
        """Finds hashes with more than one file matching, above a minimum size."""
        sql = """
            SELECT file_hash, size_bytes, COUNT(*) as count
            FROM scanned_items
            WHERE file_hash IS NOT NULL AND size_bytes >= ?
            GROUP BY file_hash, size_bytes
            HAVING COUNT(*) > 1
        """
        cursor = self.execute_sql(sql, (min_size,))
        return cursor.fetchall() if cursor else []

    def get_files_by_hash(self, file_hash: str) -> List[Path]:
        """Gets all file paths matching a specific hash."""
        sql = "SELECT path FROM scanned_items WHERE file_hash = ?"
        cursor = self.execute_sql(sql, (file_hash,))
        return [Path(row['path']) for row in cursor.fetchall()] if cursor else []

    def mark_duplicates(self, file_hash: str):
         """Marks all files with a given hash as duplicates."""
         sql = "UPDATE scanned_items SET is_duplicate = TRUE WHERE file_hash = ?"
         self.execute_sql(sql, (file_hash,))
         # Commit handled by caller or context manager

    def delete_scanned_item(self, path: Path):
        """Deletes a scanned item record from the database."""
        sql = "DELETE FROM scanned_items WHERE path = ?"
        self.execute_sql(sql, (str(path),))
        if self.conn: self.conn.commit()

    # --- Package Methods ---
    def add_packages_batch(self, packages: List[PackageInfo], scan_id: int):
        """Adds a batch of package info to the database."""
        if not self.conn: return
        sql = """
            INSERT OR REPLACE INTO packages
            (name, scan_id, version, size_bytes, description, install_date, last_used,
             is_orphan, is_dependency, required_by, optional_for)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """
        data_tuples = [
            (
                pkg.name, scan_id, pkg.version, pkg.size_bytes, pkg.description, pkg.install_date, pkg.last_used,
                pkg.is_orphan, pkg.is_dependency,
                json.dumps(pkg.required_by) if pkg.required_by else None,
                json.dumps(pkg.optional_for) if pkg.optional_for else None
            ) for pkg in packages
        ]
        try:
            with self.conn:
                self.conn.executemany(sql, data_tuples)
            logger.debug(f"Added/Replaced {len(data_tuples)} packages for scan_id {scan_id}.")
        except sqlite3.Error as e:
            logger.error(f"Error adding packages batch: {e}", exc_info=True)

    def get_packages(self, orphans_only: bool = False) -> List[PackageInfo]:
        """Retrieves package information."""
        if not self.conn: return []
        sql = """SELECT name, version, size_bytes, description, install_date, last_used,
                      is_orphan, is_dependency, required_by, optional_for
               FROM packages"""
        params = []
        if orphans_only:
            sql += " WHERE is_orphan = ?"
            params.append(True)

        cursor = self.execute_sql(sql, tuple(params))
        packages = []
        if cursor:
            for row in cursor.fetchall():
                packages.append(PackageInfo(
                    name=row['name'],
                    version=row['version'],
                    size_bytes=row['size_bytes'],
                    description=row['description'],
                    install_date=row['install_date'],
                    last_used=row['last_used'],
                    is_orphan=row['is_orphan'],
                    is_dependency=row['is_dependency'],
                    required_by=json.loads(row['required_by']) if row['required_by'] else [],
                    optional_for=json.loads(row['optional_for']) if row['optional_for'] else []
                ))
        return packages

    def delete_package(self, name: str):
        """Deletes a package record from the database."""
        sql = "DELETE FROM packages WHERE name = ?"
        self.execute_sql(sql, (name,))
        if self.conn: self.conn.commit()


    # --- Feedback Methods ---
    def add_feedback(self, feedback: ActionFeedback):
        """Adds user feedback to the database."""
        if not self.conn: return
        sql = """
            INSERT INTO action_feedback
            (suggestion_id, suggestion_type, item_details, action_taken, timestamp, user_comment)
            VALUES (?, ?, ?, ?, ?, ?)
        """
        params = (
            feedback.suggestion_id, feedback.suggestion_type, feedback.item_details,
            feedback.action_taken, feedback.timestamp, feedback.user_comment
        )
        try:
            with self.conn:
                self.execute_sql(sql, params)
            logger.debug(f"Added feedback for suggestion {feedback.suggestion_id}")
        except sqlite3.Error as e:
            logger.error(f"Error adding feedback: {e}", exc_info=True)

    def get_feedback(self, limit: int = 100) -> List[ActionFeedback]:
        """Retrieves recent action feedback."""
        if not self.conn: return []
        sql = """
            SELECT suggestion_id, suggestion_type, item_details, action_taken, timestamp, user_comment
            FROM action_feedback
            ORDER BY timestamp DESC
            LIMIT ?
        """
        cursor = self.execute_sql(sql, (limit,))
        feedback_list = []
        if cursor:
            for row in cursor.fetchall():
                 # Need suggestion_type and item_details to reconstruct ActionFeedback fully
                 # Assuming they are stored correctly
                 feedback_list.append(ActionFeedback(
                     suggestion_id=row['suggestion_id'],
                     suggestion_type=row['suggestion_type'], # Added field
                     item_details=row['item_details'],       # Added field
                     action_taken=row['action_taken'],
                     timestamp=row['timestamp'],
                     user_comment=row['user_comment']
                 ))
        return feedback_list

    # Removed Suggestion Persistence Methods

    def close(self):
        """Closes the database connection."""
        if self.conn:
            self.conn.close()
            logger.info("Database connection closed.")
            self.conn = None

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()


# Example Usage
if __name__ == '__main__':
    logging.basicConfig(level=logging.DEBUG, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
    # Create a temporary DB for testing
    db_file = Path("./temp_test_cleaner.db")
    if db_file.exists():
        db_file.unlink()

    try:
        with DatabaseManager(db_file) as db:
            print("Database Manager Initialized")

            # --- Test Scan History ---
            scan_id = db.start_scan()
            print(f"Started scan with ID: {scan_id}")
            if scan_id:
                time.sleep(0.1) # Simulate work
                db.end_scan(scan_id, 150, errors=None)
                print("Ended scan.")
            last_scan = db.get_last_scan_time()
            print(f"Last scan time: {last_scan}")

            # --- Test Scanned Items ---
            items = [
                ScannedItem(path=Path("/tmp/file1.txt"), size_bytes=1024, last_accessed=time.time()-86400*5, last_modified=time.time()-86400*10, item_type='file', extra_info={'hash': 'abc'}),
                ScannedItem(path=Path("/home/user/.cache/app/cache.dat"), size_bytes=204800, last_accessed=time.time()-86400*2, last_modified=time.time()-86400*2, item_type='cache'),
                ScannedItem(path=Path("/tmp/file2_dup.txt"), size_bytes=1024, last_accessed=time.time()-86400, last_modified=time.time()-86400*3, item_type='file', extra_info={'hash': 'abc'}),
            ]
            if scan_id:
                db.add_scanned_items_batch(items, scan_id)
                print(f"Added {len(items)} scanned items.")

            retrieved_items = db.get_scanned_items(item_type='file')
            print(f"Retrieved files: {len(retrieved_items)}")
            # print(retrieved_items)

            potential_dups = db.find_potential_duplicates(min_size=500)
            print(f"Potential duplicates: {potential_dups}")
            if potential_dups:
                dup_hash = potential_dups[0][0]
                dup_paths = db.get_files_by_hash(dup_hash)
                print(f"Files with hash {dup_hash}: {dup_paths}")
                db.mark_duplicates(dup_hash)
                print(f"Marked hash {dup_hash} as duplicate.")


            # --- Test Packages ---
            pkgs = [
                PackageInfo(name='test-pkg', version='1.0', size_bytes=500000, description='A test package', is_orphan=False),
                PackageInfo(name='orphan-pkg', version='2.1', size_bytes=100000, description='An orphan', is_orphan=True),
            ]
            if scan_id:
                db.add_packages_batch(pkgs, scan_id)
                print(f"Added {len(pkgs)} packages.")

            retrieved_pkgs = db.get_packages()
            print(f"Retrieved packages: {len(retrieved_pkgs)}")
            retrieved_orphans = db.get_packages(orphans_only=True)
            print(f"Retrieved orphans: {len(retrieved_orphans)}")
            # print(retrieved_orphans)

            # --- Test Feedback ---
            feedback = ActionFeedback(suggestion_id='sugg-123', suggestion_type='OLD_FILE', item_details='/tmp/file1.txt', action_taken='APPROVED', timestamp=time.time())
            db.add_feedback(feedback)
            print("Added feedback.")
            retrieved_feedback = db.get_feedback(limit=5)
            print(f"Retrieved feedback: {len(retrieved_feedback)}")
            # print(retrieved_feedback)

            # --- Test Deletion ---
            db.delete_scanned_item(Path("/tmp/file1.txt"))
            print("Deleted scanned item /tmp/file1.txt")
            retrieved_items_after_del = db.get_scanned_items(item_type='file')
            print(f"Retrieved files after delete: {len(retrieved_items_after_del)}")

            db.delete_package('test-pkg')
            print("Deleted package test-pkg")
            retrieved_pkgs_after_del = db.get_packages()
            print(f"Retrieved packages after delete: {len(retrieved_pkgs_after_del)}")

            # --- Test Clearing Scan Data ---
            if scan_id:
                db.clear_scan_data(scan_id)
                print(f"Cleared data for scan_id {scan_id}")
                items_after_clear = db.get_scanned_items()
                pkgs_after_clear = db.get_packages()
                print(f"Items after clear: {len(items_after_clear)}, Packages after clear: {len(pkgs_after_clear)}")


    except Exception as e:
        print(f"An error occurred: {e}")
        logger.exception("Error during DatabaseManager example usage")
    finally:
        # Clean up the temporary DB file
        if db_file.exists():
            db_file.unlink()
            print(f"Removed temporary database: {db_file}")
