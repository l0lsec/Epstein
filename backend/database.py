"""
Database Module
Handles SQLite for metadata, full-text search, and vector search
"""

import os
import re
import json
import hashlib
import sqlite3
import pickle
import threading
import queue
import time
from pathlib import Path
from typing import List, Dict, Any, Optional
from contextlib import contextmanager
import numpy as np

try:
    from backend.extractor import extract_email_date
except ImportError:
    from extractor import extract_email_date


class Database:
    """SQLite database for document metadata and search"""
    
    def __init__(self, db_path: str):
        self.db_path = db_path
        # Telemetry lives in its OWN database file so its constant per-request writes never
        # bloat the main DB's WAL. On a large DB under sustained read load the WAL cannot
        # checkpoint (a reader snapshot is always held), so co-locating telemetry made the
        # main WAL grow without bound and froze every read. See _wal_checkpointer below.
        self.telemetry_db_path = self._derive_telemetry_path(db_path)
        # Persistent connection – PRAGMAs are set once, not per call
        self._conn = sqlite3.connect(db_path, timeout=30.0, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA busy_timeout=30000")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.execute("PRAGMA cache_size=-65536")
        self._conn.execute("PRAGMA temp_store=MEMORY")
        self._conn.execute("PRAGMA mmap_size=268435456")  # let the writer use the page cache too
        self._conn.execute("PRAGMA journal_size_limit=67108864")  # truncate WAL back to <=64MB on checkpoint
        self._lock = threading.Lock()

        self._read_local = threading.local()
        # Short-TTL cache for telemetry read queries (append-only data; dashboard tolerates
        # slight staleness), so repeated admin loads don't re-scan the large telemetry table.
        self._tel_cache = {}
        self._tel_cache_ttl = 60.0
        self._tel_cache_lock = threading.Lock()
        self._telemetry_q = queue.Queue(maxsize=2000)
        self._init_telemetry_db()
        self._telemetry_thread = threading.Thread(target=self._telemetry_flusher, daemon=True)
        self._telemetry_thread.start()
        # Periodically checkpoint+truncate the main WAL. Autocheckpoint piggybacks on a writer
        # commit and is defeated by ever-present concurrent readers, so the WAL can balloon;
        # this dedicated thread guarantees it gets reset.
        self._checkpoint_thread = threading.Thread(target=self._wal_checkpointer, daemon=True)
        self._checkpoint_thread.start()
        self._init_db()
    
    @contextmanager
    def get_connection(self):
        """Return the persistent connection under a lock (thread-safe, no open/close overhead)."""
        with self._lock:
            yield self._conn

    @contextmanager
    def get_read_connection(self, timeout_seconds: float = 0):
        """Return a per-thread read-only connection. WAL mode allows concurrent readers.
        If timeout_seconds > 0, queries that exceed the time limit raise OperationalError."""
        conn = getattr(self._read_local, 'conn', None)
        if conn is None:
            conn = sqlite3.connect(self.db_path, timeout=30.0, check_same_thread=False)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA query_only=ON")
            conn.execute("PRAGMA busy_timeout=30000")
            conn.execute("PRAGMA cache_size=-65536")
            conn.execute("PRAGMA temp_store=MEMORY")
            conn.execute("PRAGMA mmap_size=268435456")
            conn.execute("PRAGMA journal_size_limit=67108864")
            self._read_local.conn = conn
        if timeout_seconds > 0:
            import time as _t
            _deadline = _t.monotonic() + timeout_seconds
            _check_count = [0]
            def _check():
                _check_count[0] += 1
                if _t.monotonic() > _deadline:
                    return 1
                return 0
            conn.set_progress_handler(_check, 2000)
        try:
            yield conn
        except sqlite3.OperationalError:
            raise
        finally:
            if timeout_seconds > 0:
                conn.set_progress_handler(None, 0)

    @staticmethod
    def _derive_telemetry_path(db_path: str) -> str:
        """Sibling telemetry DB next to the main DB (e.g. /opt/epstein/telemetry.db)."""
        if db_path == ":memory:":
            return ":memory:"
        d = os.path.dirname(os.path.abspath(db_path))
        return os.path.join(d, "telemetry.db")

    def _init_telemetry_db(self):
        """Create the separate telemetry database file and its schema."""
        conn = sqlite3.connect(self.telemetry_db_path, timeout=30.0, check_same_thread=False)
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.execute("PRAGMA busy_timeout=10000")
            conn.execute("PRAGMA journal_size_limit=67108864")
            conn.execute("""
                CREATE TABLE IF NOT EXISTS telemetry_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp TEXT NOT NULL,
                    log_source TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    client_ip TEXT,
                    method TEXT,
                    path TEXT,
                    status_code INTEGER,
                    duration_ms REAL,
                    severity TEXT,
                    data TEXT
                )
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_tel_timestamp ON telemetry_events(timestamp)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_tel_source_type ON telemetry_events(log_source, event_type)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_tel_event_type ON telemetry_events(event_type, timestamp)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_tel_ip ON telemetry_events(client_ip)")
            # Composite indexes for the admin telemetry dashboard aggregations (filter by
            # log_source, then window/group by timestamp/ip/status/path).
            conn.execute("CREATE INDEX IF NOT EXISTS idx_tel_src_ts ON telemetry_events(log_source, timestamp)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_tel_src_ip ON telemetry_events(log_source, client_ip)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_tel_src_status ON telemetry_events(log_source, status_code)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_tel_src_path ON telemetry_events(log_source, path)")
            conn.commit()
        finally:
            conn.close()

    def get_telemetry_read_connection(self):
        """Per-thread read-only connection to the telemetry database."""
        conn = getattr(self._read_local, 'tel_conn', None)
        if conn is None:
            conn = sqlite3.connect(self.telemetry_db_path, timeout=30.0, check_same_thread=False)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA query_only=ON")
            conn.execute("PRAGMA busy_timeout=10000")
            self._read_local.tel_conn = conn
        return conn

    def _wal_checkpointer(self):
        """Background thread: periodically checkpoint+truncate the main DB WAL so it can
        never grow without bound under sustained concurrent read load."""
        import time as _t
        conn = sqlite3.connect(self.db_path, timeout=60.0, check_same_thread=False)
        conn.execute("PRAGMA busy_timeout=60000")
        conn.execute("PRAGMA journal_size_limit=67108864")
        while True:
            _t.sleep(30)
            try:
                conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            except Exception:
                pass

    def _init_db(self):
        """Initialize database schema"""
        with self.get_connection() as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS documents (
                    id TEXT PRIMARY KEY,
                    filename TEXT NOT NULL,
                    original_filename TEXT,
                    path TEXT NOT NULL,
                    category TEXT,
                    subcategory TEXT,
                    file_type TEXT DEFAULT 'pdf',
                    page_count INTEGER DEFAULT 0,
                    char_count INTEGER DEFAULT 0,
                    duration_seconds REAL,
                    full_text TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
                
                CREATE INDEX IF NOT EXISTS idx_documents_category ON documents(category);
                CREATE INDEX IF NOT EXISTS idx_documents_subcategory ON documents(subcategory);
                CREATE INDEX IF NOT EXISTS idx_documents_file_type ON documents(file_type);
                
                CREATE VIRTUAL TABLE IF NOT EXISTS documents_fts USING fts5(
                    id,
                    filename,
                    full_text,
                    content=documents,
                    content_rowid=rowid
                );
                
                CREATE TRIGGER IF NOT EXISTS documents_ai AFTER INSERT ON documents BEGIN
                    INSERT INTO documents_fts(rowid, id, filename, full_text)
                    VALUES (new.rowid, new.id, new.filename, new.full_text);
                END;
                
                CREATE TRIGGER IF NOT EXISTS documents_ad AFTER DELETE ON documents BEGIN
                    INSERT INTO documents_fts(documents_fts, rowid, id, filename, full_text)
                    VALUES('delete', old.rowid, old.id, old.filename, old.full_text);
                END;
                
                -- Scoped to FTS columns only: AFTER UPDATE OF ... means this trigger does
                -- NOT fire for metadata-only updates (is_hidden, file_type), which would
                -- otherwise re-tokenize the entire full_text blob and freeze the write lock.
                CREATE TRIGGER IF NOT EXISTS documents_au
                AFTER UPDATE OF id, filename, full_text ON documents
                WHEN old.full_text IS NOT new.full_text
                     OR old.filename IS NOT new.filename
                     OR old.id IS NOT new.id
                BEGIN
                    INSERT INTO documents_fts(documents_fts, rowid, id, filename, full_text)
                    VALUES('delete', old.rowid, old.id, old.filename, old.full_text);
                    INSERT INTO documents_fts(rowid, id, filename, full_text)
                    VALUES (new.rowid, new.id, new.filename, new.full_text);
                END;
                
                -- Table for caching AI-generated summaries
                CREATE TABLE IF NOT EXISTS summaries (
                    document_id TEXT PRIMARY KEY,
                    summary TEXT NOT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (document_id) REFERENCES documents(id)
                );
            """)
            conn.commit()
            
            # Migration: Add duration_seconds column if it doesn't exist
            try:
                conn.execute("SELECT duration_seconds FROM documents LIMIT 1")
            except:
                conn.execute("ALTER TABLE documents ADD COLUMN duration_seconds REAL")
                conn.commit()
                print("✅ Added duration_seconds column to documents table")
            
            # Migration: Add document_date column if it doesn't exist
            try:
                conn.execute("SELECT document_date FROM documents LIMIT 1")
            except:
                conn.execute("ALTER TABLE documents ADD COLUMN document_date TEXT")
                conn.execute("CREATE INDEX IF NOT EXISTS idx_documents_date ON documents(document_date)")
                conn.commit()
                print("✅ Added document_date column to documents table")
            
            # Migration: Create summaries table if it doesn't exist (for existing databases)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS summaries (
                    document_id TEXT PRIMARY KEY,
                    summary TEXT NOT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (document_id) REFERENCES documents(id)
                )
            """)
            conn.commit()
            
            # Settings table for app-wide settings (e.g., AI visibility)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS settings (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            conn.commit()
            
            # Pinned documents table for controversial/featured documents
            conn.execute("""
                CREATE TABLE IF NOT EXISTS pinned_documents (
                    document_id TEXT PRIMARY KEY,
                    reason TEXT,
                    display_order INTEGER DEFAULT 0,
                    pinned_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (document_id) REFERENCES documents(id)
                )
            """)
            conn.commit()
            
            # Keywords table for dynamic topic/keyword filtering
            conn.execute("""
                CREATE TABLE IF NOT EXISTS keywords (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL UNIQUE,
                    search_term TEXT NOT NULL,
                    category TEXT NOT NULL,
                    document_count INTEGER DEFAULT 0,
                    display_order INTEGER DEFAULT 0,
                    is_active INTEGER DEFAULT 1,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_keywords_category ON keywords(category)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_keywords_active ON keywords(is_active)")
            conn.commit()
            
            # Missing documents table for tracking 404s from DOJ website
            conn.execute("""
                CREATE TABLE IF NOT EXISTS missing_documents (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    filename TEXT NOT NULL,
                    url TEXT NOT NULL,
                    dataset_num INTEGER NOT NULL,
                    page_found_on INTEGER,
                    first_seen TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    last_checked TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    check_count INTEGER DEFAULT 1,
                    UNIQUE(filename, dataset_num)
                )
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_missing_dataset ON missing_documents(dataset_num)")
            conn.commit()
            
            # DOJ manifest table for tracking all files found on website (completeness tracking)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS doj_manifest (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    filename TEXT NOT NULL,
                    url TEXT NOT NULL,
                    dataset_num INTEGER NOT NULL,
                    page_found_on INTEGER,
                    status TEXT DEFAULT 'found',
                    first_seen TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    last_updated TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(filename, dataset_num)
                )
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_manifest_dataset ON doj_manifest(dataset_num)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_manifest_status ON doj_manifest(status)")
            conn.commit()
            
            # Hidden categories table for visibility control
            conn.execute("""
                CREATE TABLE IF NOT EXISTS hidden_categories (
                    category TEXT PRIMARY KEY,
                    hidden_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            conn.commit()
            
            # Migration: Add is_hidden column if it doesn't exist
            try:
                conn.execute("SELECT is_hidden FROM documents LIMIT 1")
            except:
                conn.execute("ALTER TABLE documents ADD COLUMN is_hidden INTEGER DEFAULT 0")
                conn.execute("CREATE INDEX IF NOT EXISTS idx_documents_hidden ON documents(is_hidden)")
                conn.commit()
                print("✅ Added is_hidden column to documents table")
            
            # Migration: Normalize is_hidden NULLs to 0 for clean index equality scans
            conn.execute("UPDATE documents SET is_hidden = 0 WHERE is_hidden IS NULL")
            conn.commit()

            # Migration: rescope the FTS update trigger on pre-existing databases.
            # The original documents_au fired AFTER UPDATE on ANY column and re-tokenized the
            # entire full_text into FTS5 — so a metadata-only write like "UPDATE documents SET
            # is_hidden = 1" did seconds of pointless FTS work while holding the global write
            # lock, freezing the server. CREATE TRIGGER IF NOT EXISTS above can't replace an
            # existing trigger, so detect the old form and recreate it scoped to FTS columns.
            try:
                row = conn.execute(
                    "SELECT sql FROM sqlite_master WHERE type='trigger' AND name='documents_au'"
                ).fetchone()
                existing_sql = row[0] if row else ""
                if existing_sql and "UPDATE OF" not in existing_sql:
                    conn.execute("DROP TRIGGER IF EXISTS documents_au")
                    conn.execute("""
                        CREATE TRIGGER documents_au
                        AFTER UPDATE OF id, filename, full_text ON documents
                        WHEN old.full_text IS NOT new.full_text
                             OR old.filename IS NOT new.filename
                             OR old.id IS NOT new.id
                        BEGIN
                            INSERT INTO documents_fts(documents_fts, rowid, id, filename, full_text)
                            VALUES('delete', old.rowid, old.id, old.filename, old.full_text);
                            INSERT INTO documents_fts(rowid, id, filename, full_text)
                            VALUES (new.rowid, new.id, new.filename, new.full_text);
                        END;
                    """)
                    conn.commit()
                    print("✅ Rescoped documents_au FTS trigger to FTS columns only (was firing on every update)")
            except Exception as e:
                print(f"Note: documents_au trigger migration skipped: {e}")

            # Migration: Reclassify image-format files that contain OCR text as "document"
            try:
                cursor = conn.execute(
                    "SELECT COUNT(*) FROM documents WHERE file_type = 'image' AND char_count > 50"
                )
                count = cursor.fetchone()[0]
                if count > 0:
                    conn.execute("""
                        UPDATE documents SET file_type = 'document'
                        WHERE file_type = 'image' AND char_count > 50
                    """)
                    conn.commit()
                    print(f"✅ Reclassified {count} scanned documents (image -> document)")
            except Exception as e:
                print(f"Note: image reclassification skipped: {e}")
            
            # Covering index for browse pagination: includes ALL columns selected by the
            # browse query so SQLite can serve it entirely from the index without touching
            # the main table (which has huge full_text blobs that make row lookups slow).
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_browse_covering "
                "ON documents(is_hidden, filename, id, path, category, subcategory, "
                "file_type, page_count, char_count, duration_seconds)"
            )
            conn.commit()

            # Compact index for category aggregation on visible docs only
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_docs_visible_category "
                "ON documents(category) WHERE is_hidden = 0"
            )
            conn.commit()

            # Composite index for stats GROUP BY category, subcategory
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_docs_category_subcategory "
                "ON documents(category, subcategory)"
            )
            conn.commit()
            
            # Drop the old narrower index — the covering index supersedes it
            conn.execute("DROP INDEX IF EXISTS idx_documents_hidden_filename")
            conn.commit()

            # Telemetry now lives in a SEPARATE database file (see _init_telemetry_db) so its
            # per-request writes don't bloat this DB's WAL. Any legacy telemetry_events table
            # in this file is left in place (untouched) and simply no longer read/written.

    # ── Telemetry helpers ──

    def insert_telemetry_event(self, timestamp: str, log_source: str, event_type: str,
                                client_ip: str = None, method: str = None, path: str = None,
                                status_code: int = None, duration_ms: float = None,
                                severity: str = None, data: dict = None):
        """Queue a telemetry event for async background insertion (non-blocking)."""
        row = (timestamp, log_source, event_type, client_ip, method, path,
               status_code, duration_ms, severity,
               json.dumps(data) if data else None)
        try:
            self._telemetry_q.put_nowait(row)
        except queue.Full:
            pass

    def _telemetry_flusher(self):
        """Background thread: drains the telemetry queue and batch-inserts into the telemetry DB."""
        conn = sqlite3.connect(self.telemetry_db_path, timeout=30.0, check_same_thread=False)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA busy_timeout=10000")
        conn.execute("PRAGMA journal_size_limit=67108864")
        sql = ("INSERT INTO telemetry_events "
               "(timestamp, log_source, event_type, client_ip, method, path, "
               "status_code, duration_ms, severity, data) "
               "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)")
        while True:
            batch = []
            try:
                batch.append(self._telemetry_q.get(timeout=2.0))
            except queue.Empty:
                continue
            while len(batch) < 100:
                try:
                    batch.append(self._telemetry_q.get_nowait())
                except queue.Empty:
                    break
            try:
                conn.executemany(sql, batch)
                conn.commit()
            except Exception:
                pass

    def insert_telemetry_batch(self, rows: list):
        """Batch-insert telemetry rows into the telemetry DB. Each row matches the column order."""
        if not rows:
            return 0
        conn = sqlite3.connect(self.telemetry_db_path, timeout=30.0, check_same_thread=False)
        try:
            conn.execute("PRAGMA busy_timeout=10000")
            conn.executemany(
                """INSERT INTO telemetry_events
                   (timestamp, log_source, event_type, client_ip, method, path,
                    status_code, duration_ms, severity, data)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                rows
            )
            conn.commit()
        finally:
            conn.close()
        return len(rows)

    def query_telemetry(self, sql: str, params: tuple = (), timeout_seconds: float = 0) -> list:
        """Run a read-only SQL query against the telemetry DB and return list of dicts.
        Results are cached for a short TTL (telemetry is append-only; the admin dashboard
        tolerates slight staleness) so repeated dashboard loads don't re-scan the large table.
        If timeout_seconds > 0, a query exceeding it is interrupted (raises OperationalError)."""
        key = (sql, params)
        now = time.monotonic()
        with self._tel_cache_lock:
            hit = self._tel_cache.get(key)
            if hit is not None and hit[1] > now:
                return hit[0]
        conn = self.get_telemetry_read_connection()
        if timeout_seconds > 0:
            _deadline = now + timeout_seconds
            conn.set_progress_handler(lambda: 1 if time.monotonic() > _deadline else 0, 2000)
        try:
            cursor = conn.execute(sql, params)
            rows = [dict(row) for row in cursor.fetchall()]
        finally:
            if timeout_seconds > 0:
                conn.set_progress_handler(None, 0)
        with self._tel_cache_lock:
            self._tel_cache[key] = (rows, now + self._tel_cache_ttl)
            if len(self._tel_cache) > 1000:
                self._tel_cache = {k: v for k, v in self._tel_cache.items() if v[1] > now}
        return rows

    def clear_telemetry(self, log_source: str = None):
        """Delete telemetry rows from the telemetry DB, optionally filtered by log_source."""
        conn = sqlite3.connect(self.telemetry_db_path, timeout=30.0, check_same_thread=False)
        try:
            conn.execute("PRAGMA busy_timeout=10000")
            if log_source:
                conn.execute("DELETE FROM telemetry_events WHERE log_source = ?", (log_source,))
            else:
                conn.execute("DELETE FROM telemetry_events")
            conn.commit()
        finally:
            conn.close()

    def rebuild_fts(self, progress_callback=None):
        """Rebuild the FTS5 index to fix sync issues.

        progress_callback: optional callable(phase, current, total, message)
            phase: 1=drop, 2=create table, 3=populate, 4=triggers
            current/total: progress during phase 3 (documents), else 0/total_steps
        """
        batch_size = 500
        with self.get_connection() as conn:
            # Phase 1: Drop triggers and FTS table
            if progress_callback:
                progress_callback(1, 0, 4, "Dropping old FTS index and triggers...")
            conn.executescript("""
                DROP TRIGGER IF EXISTS documents_ai;
                DROP TRIGGER IF EXISTS documents_ad;
                DROP TRIGGER IF EXISTS documents_au;
                DROP TABLE IF EXISTS documents_fts;
            """)
            conn.commit()

            # Phase 2: Create empty FTS table
            if progress_callback:
                progress_callback(2, 1, 4, "Creating FTS table...")
            conn.executescript("""
                CREATE VIRTUAL TABLE documents_fts USING fts5(
                    id,
                    filename,
                    full_text,
                    content=documents,
                    content_rowid=rowid
                );
            """)
            conn.commit()

            # Phase 3: Repopulate in batches for progress reporting
            total = conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0]
            if progress_callback:
                progress_callback(3, 0, total, "Populating FTS index...")
            offset = 0
            while offset < total:
                conn.execute("""
                    INSERT INTO documents_fts(rowid, id, filename, full_text)
                    SELECT rowid, id, filename, full_text FROM documents
                    ORDER BY rowid LIMIT ? OFFSET ?
                """, (batch_size, offset))
                offset += batch_size
                if progress_callback:
                    progress_callback(3, min(offset, total), total, "Populating FTS index...")
            conn.commit()

            # Phase 4: Recreate triggers
            if progress_callback:
                progress_callback(4, 4, 4, "Creating triggers...")
            conn.executescript("""
                CREATE TRIGGER documents_ai AFTER INSERT ON documents BEGIN
                    INSERT INTO documents_fts(rowid, id, filename, full_text)
                    VALUES (new.rowid, new.id, new.filename, new.full_text);
                END;
                CREATE TRIGGER documents_ad AFTER DELETE ON documents BEGIN
                    INSERT INTO documents_fts(documents_fts, rowid, id, filename, full_text)
                    VALUES('delete', old.rowid, old.id, old.filename, old.full_text);
                END;
                CREATE TRIGGER documents_au AFTER UPDATE ON documents BEGIN
                    INSERT INTO documents_fts(documents_fts, rowid, id, filename, full_text)
                    VALUES('delete', old.rowid, old.id, old.filename, old.full_text);
                    INSERT INTO documents_fts(rowid, id, filename, full_text)
                    VALUES (new.rowid, new.id, new.filename, new.full_text);
                END;
            """)
            conn.commit()
        if not progress_callback:
            print("FTS index rebuilt successfully")
    
    def insert_document(self, doc: Dict[str, Any]):
        """Insert or update a document"""
        # Extract date from email headers if not already provided
        document_date = doc.get("document_date")
        if not document_date:
            full_text = doc.get("full_text", "")
            if full_text:
                document_date = extract_email_date(full_text)
        
        with self.get_connection() as conn:
            conn.execute("""
                INSERT OR REPLACE INTO documents 
                (id, filename, original_filename, path, category, subcategory, 
                 file_type, page_count, char_count, duration_seconds, full_text, document_date)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                doc["id"],
                doc["filename"],
                doc.get("original_filename", doc["filename"]),
                doc["path"],
                doc.get("category", "Unknown"),
                doc.get("subcategory", ""),
                doc.get("file_type", "pdf"),
                doc.get("page_count", 0),
                doc.get("char_count", 0),
                doc.get("duration_seconds"),
                doc.get("full_text", ""),
                document_date
            ))
            conn.commit()
    
    def search_fulltext(self, query: str, limit: int = 50, offset: int = 0, 
                        category: Optional[str] = None,
                        subcategory: Optional[str] = None,
                        file_type: Optional[str] = None,
                        date_from: Optional[str] = None,
                        date_to: Optional[str] = None,
                        include_hidden: bool = False) -> List[Dict[str, Any]]:
        """Full-text search across documents
        
        Args:
            include_hidden: If False (default), excludes hidden documents and hidden categories
        """
        with self.get_read_connection(timeout_seconds=15) as conn:
            # Build query with optional filters
            sql = """
                SELECT 
                    d.id, d.filename, d.path, d.category, d.subcategory, d.file_type,
                    d.page_count, d.char_count, d.duration_seconds, d.document_date,
                    snippet(documents_fts, 2, '<mark>', '</mark>', '...', 64) as snippet,
                    bm25(documents_fts) as score
                FROM documents_fts
                JOIN documents d ON documents_fts.id = d.id
                LEFT JOIN hidden_categories hc ON d.category = hc.category
                WHERE documents_fts MATCH ?
            """
            params = [query]
            
            # Filter out hidden documents and categories unless include_hidden is True
            if not include_hidden:
                sql += " AND (d.is_hidden IS NULL OR d.is_hidden = 0) AND hc.category IS NULL"
                sql += " AND d.subcategory != 'thumbnails'"
            
            if category:
                sql += " AND d.category = ?"
                params.append(category)
            
            if subcategory:
                sql += " AND d.subcategory = ?"
                params.append(subcategory)
            
            if file_type:
                sql += " AND d.file_type = ?"
                params.append(file_type)
            
            if date_from:
                sql += " AND d.document_date >= ?"
                params.append(date_from)
            
            if date_to:
                sql += " AND d.document_date <= ?"
                params.append(date_to)
            
            sql += " ORDER BY score LIMIT ? OFFSET ?"
            params.extend([limit, offset])
            
            cursor = conn.execute(sql, params)
            results = []
            for row in cursor:
                results.append(dict(row))
            return results
    
    def count_fulltext_results(self, query: str, 
                               category: Optional[str] = None,
                               subcategory: Optional[str] = None,
                               file_type: Optional[str] = None,
                               date_from: Optional[str] = None,
                               date_to: Optional[str] = None,
                               include_hidden: bool = False) -> int:
        """Count total full-text search results (for pagination)
        
        Args:
            include_hidden: If False (default), excludes hidden documents and hidden categories
        """
        with self.get_read_connection(timeout_seconds=10) as conn:
            sql = """
                SELECT COUNT(*)
                FROM documents_fts
                JOIN documents d ON documents_fts.id = d.id
                LEFT JOIN hidden_categories hc ON d.category = hc.category
                WHERE documents_fts MATCH ?
            """
            params = [query]
            
            # Filter out hidden documents and categories unless include_hidden is True
            if not include_hidden:
                sql += " AND (d.is_hidden IS NULL OR d.is_hidden = 0) AND hc.category IS NULL"
                sql += " AND d.subcategory != 'thumbnails'"
            
            if category:
                sql += " AND d.category = ?"
                params.append(category)
            
            if subcategory:
                sql += " AND d.subcategory = ?"
                params.append(subcategory)
            
            if file_type:
                sql += " AND d.file_type = ?"
                params.append(file_type)
            
            if date_from:
                sql += " AND d.document_date >= ?"
                params.append(date_from)
            
            if date_to:
                sql += " AND d.document_date <= ?"
                params.append(date_to)
            
            cursor = conn.execute(sql, params)
            return cursor.fetchone()[0]
    
    def get_search_facets(self, query: str, 
                          category: Optional[str] = None,
                          subcategory: Optional[str] = None,
                          file_type: Optional[str] = None,
                          date_from: Optional[str] = None,
                          date_to: Optional[str] = None,
                          include_hidden: bool = False) -> Dict[str, Any]:
        """Get faceted counts for search results (category, subcategory, file_type breakdowns)
        
        Args:
            include_hidden: If False (default), excludes hidden documents and hidden categories
        """
        with self.get_read_connection(timeout_seconds=10) as conn:
            # Base match condition
            base_match = "documents_fts MATCH ?"
            base_params = [query]
            
            # Visibility filter
            visibility_filter = ""
            if not include_hidden:
                visibility_filter = " AND (d.is_hidden IS NULL OR d.is_hidden = 0) AND hc.category IS NULL AND d.subcategory != 'thumbnails'"
            
            # Build date filter conditions
            date_filter = ""
            date_params = []
            if date_from:
                date_filter += " AND d.document_date >= ?"
                date_params.append(date_from)
            if date_to:
                date_filter += " AND d.document_date <= ?"
                date_params.append(date_to)
            
            # Category counts (unfiltered by category to show all options)
            category_sql = f"""
                SELECT d.category, COUNT(*) as count
                FROM documents_fts
                JOIN documents d ON documents_fts.id = d.id
                LEFT JOIN hidden_categories hc ON d.category = hc.category
                WHERE {base_match}{visibility_filter}
            """
            category_params = list(base_params)
            if file_type:
                category_sql += " AND d.file_type = ?"
                category_params.append(file_type)
            category_sql += date_filter
            category_params.extend(date_params)
            category_sql += " GROUP BY d.category ORDER BY count DESC"
            
            cursor = conn.execute(category_sql, category_params)
            categories = [{"category": row[0], "count": row[1]} for row in cursor.fetchall()]
            
            # Subcategory counts (filtered by current category if selected)
            subcategories = []
            if category:
                subcategory_sql = f"""
                    SELECT d.subcategory, COUNT(*) as count
                    FROM documents_fts
                    JOIN documents d ON documents_fts.id = d.id
                    LEFT JOIN hidden_categories hc ON d.category = hc.category
                    WHERE {base_match}{visibility_filter} AND d.category = ?
                """
                subcategory_params = list(base_params) + [category]
                if file_type:
                    subcategory_sql += " AND d.file_type = ?"
                    subcategory_params.append(file_type)
                subcategory_sql += date_filter
                subcategory_params.extend(date_params)
                subcategory_sql += " GROUP BY d.subcategory ORDER BY count DESC"
                
                cursor = conn.execute(subcategory_sql, subcategory_params)
                subcategories = [{"subcategory": row[0], "count": row[1]} for row in cursor.fetchall() if row[0]]
            
            # File type counts (unfiltered by file_type to show all options)
            file_type_sql = f"""
                SELECT d.file_type, COUNT(*) as count
                FROM documents_fts
                JOIN documents d ON documents_fts.id = d.id
                LEFT JOIN hidden_categories hc ON d.category = hc.category
                WHERE {base_match}{visibility_filter}
            """
            file_type_params = list(base_params)
            if category:
                file_type_sql += " AND d.category = ?"
                file_type_params.append(category)
            if subcategory:
                file_type_sql += " AND d.subcategory = ?"
                file_type_params.append(subcategory)
            file_type_sql += date_filter
            file_type_params.extend(date_params)
            file_type_sql += " GROUP BY d.file_type ORDER BY count DESC"
            
            cursor = conn.execute(file_type_sql, file_type_params)
            file_types = [{"file_type": row[0], "count": row[1]} for row in cursor.fetchall()]
            
            return {
                "categories": categories,
                "subcategories": subcategories,
                "file_types": file_types
            }
    
    def get_document(self, doc_id: str, include_hidden: bool = True, include_full_text: bool = True) -> Optional[Dict[str, Any]]:
        """Get a single document by ID
        
        Args:
            doc_id: The document ID
            include_hidden: If False, returns None for hidden documents or documents in hidden categories
            include_full_text: If False, omits full_text (faster and smaller for list/search use)
        """
        with self.get_read_connection() as conn:
            if include_full_text:
                if include_hidden:
                    cursor = conn.execute(
                        "SELECT * FROM documents WHERE id = ?", (doc_id,)
                    )
                else:
                    cursor = conn.execute("""
                        SELECT d.* 
                        FROM documents d
                        LEFT JOIN hidden_categories hc ON d.category = hc.category
                        WHERE d.id = ? 
                          AND (d.is_hidden IS NULL OR d.is_hidden = 0)
                          AND hc.category IS NULL
                    """, (doc_id,))
            else:
                cols = "d.id, d.filename, d.original_filename, d.path, d.category, d.subcategory, d.file_type, d.page_count, d.char_count, d.duration_seconds, d.document_date, d.created_at"
                if include_hidden:
                    cursor = conn.execute(
                        f"SELECT {cols} FROM documents d WHERE d.id = ?", (doc_id,)
                    )
                else:
                    cursor = conn.execute(f"""
                        SELECT {cols}
                        FROM documents d
                        LEFT JOIN hidden_categories hc ON d.category = hc.category
                        WHERE d.id = ? 
                          AND (d.is_hidden IS NULL OR d.is_hidden = 0)
                          AND hc.category IS NULL
                    """, (doc_id,))
            row = cursor.fetchone()
            return dict(row) if row else None
    
    def get_document_full_text(self, doc_id: str, include_hidden: bool = False) -> Optional[str]:
        """Get only full_text for a document (for lazy loading; avoids sending full doc)."""
        doc = self.get_document(doc_id, include_hidden=include_hidden, include_full_text=True)
        return doc.get("full_text") if doc else None
    
    def get_subcategory_counts(self, category: Optional[str] = None, include_hidden: bool = False) -> List[Dict[str, Any]]:
        """Get subcategory counts, optionally for a single category. Lighter than get_stats()."""
        with self.get_read_connection() as conn:
            if include_hidden:
                if category:
                    cursor = conn.execute("""
                        SELECT category, subcategory, COUNT(*) as count
                        FROM documents
                        WHERE category = ?
                        GROUP BY category, subcategory
                        ORDER BY category, count DESC
                    """, (category,))
                else:
                    cursor = conn.execute("""
                        SELECT category, subcategory, COUNT(*) as count
                        FROM documents
                        GROUP BY category, subcategory
                        ORDER BY category, count DESC
                    """)
            else:
                if category:
                    cursor = conn.execute("""
                        SELECT d.category, d.subcategory, COUNT(*) as count
                        FROM documents d
                        LEFT JOIN hidden_categories hc ON d.category = hc.category
                        WHERE (d.is_hidden IS NULL OR d.is_hidden = 0) AND hc.category IS NULL
                          AND d.subcategory != 'thumbnails'
                          AND d.category = ?
                        GROUP BY d.category, d.subcategory
                        ORDER BY d.category, count DESC
                    """, (category,))
                else:
                    cursor = conn.execute("""
                        SELECT d.category, d.subcategory, COUNT(*) as count
                        FROM documents d
                        LEFT JOIN hidden_categories hc ON d.category = hc.category
                        WHERE (d.is_hidden IS NULL OR d.is_hidden = 0) AND hc.category IS NULL
                          AND d.subcategory != 'thumbnails'
                        GROUP BY d.category, d.subcategory
                        ORDER BY d.category, count DESC
                    """)
            return [dict(row) for row in cursor]
    
    def get_stats(self) -> Dict[str, Any]:
        """Get database statistics"""
        with self.get_read_connection() as conn:
            stats = {}
            
            cursor = conn.execute("SELECT COUNT(*), COALESCE(SUM(page_count), 0) FROM documents")
            row = cursor.fetchone()
            stats["total_documents"] = row[0]
            stats["total_pages"] = row[1]
            
            cursor = conn.execute("""
                SELECT category, COUNT(*) as count 
                FROM documents 
                GROUP BY category 
                ORDER BY count DESC
            """)
            stats["by_category"] = [dict(row) for row in cursor]
            
            cursor = conn.execute("""
                SELECT category, subcategory, COUNT(*) as count 
                FROM documents 
                GROUP BY category, subcategory 
                ORDER BY category, count DESC
            """)
            stats["by_subcategory"] = [dict(row) for row in cursor]
            
            cursor = conn.execute("""
                SELECT file_type, COUNT(*) as count 
                FROM documents 
                GROUP BY file_type 
                ORDER BY count DESC
            """)
            stats["by_file_type"] = [dict(row) for row in cursor]
            
            return stats
    
    def get_category_counts(self, keyword: Optional[str] = None, include_hidden: bool = False) -> List[Dict[str, Any]]:
        """Get category counts, optionally filtered by keyword search

        Args:
            keyword: Optional keyword to filter by
            include_hidden: If False (default), excludes hidden categories and hidden documents
        """
        with self.get_read_connection() as conn:
            if keyword:
                # Use FTS to filter by keyword
                escaped_keyword = keyword.replace('"', '""')
                if include_hidden:
                    sql = """
                        SELECT d.category, COUNT(*) as count
                        FROM documents d
                        JOIN documents_fts fts ON d.id = fts.id
                        WHERE documents_fts MATCH ?
                        GROUP BY d.category
                        ORDER BY count DESC
                    """
                else:
                    sql = """
                        SELECT d.category, COUNT(*) as count
                        FROM documents d
                        JOIN documents_fts fts ON d.id = fts.id
                        WHERE documents_fts MATCH ?
                          AND d.is_hidden = 0
                          AND d.category NOT IN (SELECT category FROM hidden_categories)
                          AND d.subcategory != 'thumbnails'
                        GROUP BY d.category
                        ORDER BY count DESC
                    """
                cursor = conn.execute(sql, [f'"{escaped_keyword}"*'])
            else:
                if include_hidden:
                    sql = """
                        SELECT category, COUNT(*) as count 
                        FROM documents 
                        GROUP BY category 
                        ORDER BY count DESC
                    """
                    cursor = conn.execute(sql)
                else:
                    sql = """
                        SELECT d.category, COUNT(*) as count 
                        FROM documents d
                        WHERE d.is_hidden = 0
                          AND d.category NOT IN (SELECT category FROM hidden_categories)
                          AND d.subcategory != 'thumbnails'
                        GROUP BY d.category 
                        ORDER BY count DESC
                    """
                    cursor = conn.execute(sql)
            
            return [dict(row) for row in cursor]
    
    def get_all_documents(self, limit: int = 100, offset: int = 0, 
                          category: Optional[str] = None,
                          subcategory: Optional[str] = None,
                          file_type: Optional[str] = None,
                          filename: Optional[str] = None,
                          keyword: Optional[str] = None,
                          search: Optional[str] = None,
                          include_hidden: bool = False) -> List[Dict[str, Any]]:
        """Get all documents with pagination and filtering
        
        Args:
            search: Searches both filename AND subcategory (for admin document search)
            include_hidden: If False (default), excludes hidden documents and hidden categories
        """
        with self.get_read_connection() as conn:
            params = []
            conditions = []
            
            if keyword:
                sql = """
                    SELECT DISTINCT d.id, d.filename, d.path, d.category, d.subcategory, d.file_type, d.page_count, d.char_count, d.duration_seconds, d.is_hidden
                    FROM documents d
                    JOIN documents_fts fts ON d.id = fts.id
                    WHERE documents_fts MATCH ?
                """
                escaped_keyword = keyword.replace('"', '""')
                params.append(f'"{escaped_keyword}"*')
                
                if not include_hidden:
                    conditions.append("d.is_hidden = 0")
                    conditions.append("d.category NOT IN (SELECT category FROM hidden_categories)")
                    conditions.append("d.subcategory != 'thumbnails'")
            else:
                sql = """
                    SELECT d.id, d.filename, d.path, d.category, d.subcategory, d.file_type, d.page_count, d.char_count, d.duration_seconds, d.is_hidden
                    FROM documents d
                """
                if not include_hidden:
                    conditions.append("d.is_hidden = 0")
                    conditions.append("d.category NOT IN (SELECT category FROM hidden_categories)")
                    conditions.append("d.subcategory != 'thumbnails'")
            
            if category:
                conditions.append("d.category = ?")
                params.append(category)
            
            if subcategory:
                conditions.append("d.subcategory = ?")
                params.append(subcategory)
            
            if file_type:
                conditions.append("d.file_type = ?")
                params.append(file_type)
            
            if filename:
                # Case-insensitive partial match on filename
                conditions.append("LOWER(d.filename) LIKE LOWER(?)")
                params.append(f"%{filename}%")
            
            if search:
                # Search both filename and subcategory (for admin document search)
                conditions.append("(LOWER(d.filename) LIKE LOWER(?) OR LOWER(d.subcategory) LIKE LOWER(?))")
                params.append(f"%{search}%")
                params.append(f"%{search}%")
            
            if conditions:
                if keyword:
                    sql += " AND " + " AND ".join(conditions)
                else:
                    sql += " WHERE " + " AND ".join(conditions)
            
            sql += " ORDER BY d.filename LIMIT ? OFFSET ?"
            params.extend([limit, offset])
            
            cursor = conn.execute(sql, params)
            return [dict(row) for row in cursor]
    
    def get_all_documents_with_total(
        self,
        limit: int = 100,
        offset: int = 0,
        category: Optional[str] = None,
        subcategory: Optional[str] = None,
        file_type: Optional[str] = None,
        filename: Optional[str] = None,
        include_hidden: bool = False,
    ) -> tuple:
        """Get one page of documents and total count via two queries.
        
        Returns (documents_list, total_count). Uses a separate COUNT(*) query
        so the data query can short-circuit via covering index after LIMIT rows.
        """
        with self.get_read_connection() as conn:
            params = []
            conditions = []
            if not include_hidden:
                conditions.append("d.is_hidden = 0")
                conditions.append("d.category NOT IN (SELECT category FROM hidden_categories)")
                conditions.append("d.subcategory != 'thumbnails'")
            if category:
                conditions.append("d.category = ?")
                params.append(category)
            if subcategory:
                conditions.append("d.subcategory = ?")
                params.append(subcategory)
            if file_type:
                conditions.append("d.file_type = ?")
                params.append(file_type)
            if filename:
                conditions.append("LOWER(d.filename) LIKE LOWER(?)")
                params.append(f"%{filename}%")

            where_clause = (" WHERE " + " AND ".join(conditions)) if conditions else ""

            count_sql = "SELECT COUNT(*) FROM documents d" + where_clause
            cursor = conn.execute(count_sql, params)
            total = cursor.fetchone()[0]

            data_sql = (
                "SELECT d.id, d.filename, d.path, d.category, d.subcategory, d.file_type,"
                "       d.page_count, d.char_count, d.duration_seconds, d.is_hidden"
                " FROM documents d" + where_clause +
                " ORDER BY d.filename LIMIT ? OFFSET ?"
            )
            data_params = params + [limit, offset]
            cursor = conn.execute(data_sql, data_params)
            docs = [dict(row) for row in cursor]
            return (docs, total)
    
    def count_documents(self, category: Optional[str] = None,
                        subcategory: Optional[str] = None,
                        file_type: Optional[str] = None,
                        filename: Optional[str] = None,
                        keyword: Optional[str] = None,
                        include_hidden: bool = False) -> int:
        """Count documents with optional filters
        
        Args:
            include_hidden: If False (default), excludes hidden documents and hidden categories
        """
        with self.get_read_connection() as conn:
            params = []
            conditions = []
            
            if keyword:
                sql = """
                    SELECT COUNT(DISTINCT d.id)
                    FROM documents d
                    JOIN documents_fts fts ON d.id = fts.id
                    WHERE documents_fts MATCH ?
                """
                escaped_keyword = keyword.replace('"', '""')
                params.append(f'"{escaped_keyword}"*')
                
                if not include_hidden:
                    conditions.append("d.is_hidden = 0")
                    conditions.append("d.category NOT IN (SELECT category FROM hidden_categories)")
                    conditions.append("d.subcategory != 'thumbnails'")
            else:
                sql = """
                    SELECT COUNT(*) 
                    FROM documents d
                """
                
                if not include_hidden:
                    conditions.append("d.is_hidden = 0")
                    conditions.append("d.category NOT IN (SELECT category FROM hidden_categories)")
                    conditions.append("d.subcategory != 'thumbnails'")
            
            if category:
                conditions.append("d.category = ?")
                params.append(category)
            
            if subcategory:
                conditions.append("d.subcategory = ?")
                params.append(subcategory)
            
            if file_type:
                conditions.append("d.file_type = ?")
                params.append(file_type)
            
            if filename:
                conditions.append("LOWER(d.filename) LIKE LOWER(?)")
                params.append(f"%{filename}%")
            
            if conditions:
                if keyword:
                    sql += " AND " + " AND ".join(conditions)
                else:
                    sql += " WHERE " + " AND ".join(conditions)
            
            cursor = conn.execute(sql, params)
            return cursor.fetchone()[0]
    
    def get_documents_by_ids(self, doc_ids: List[str]) -> List[Dict[str, Any]]:
        """Get multiple documents by their IDs"""
        if not doc_ids:
            return []
        with self.get_connection() as conn:
            placeholders = ','.join('?' * len(doc_ids))
            cursor = conn.execute(
                f"SELECT * FROM documents WHERE id IN ({placeholders})", doc_ids
            )
            return [dict(row) for row in cursor]
    
    def get_summary(self, doc_id: str) -> Optional[Dict[str, Any]]:
        """Get a cached summary for a document"""
        with self.get_read_connection() as conn:
            cursor = conn.execute(
                "SELECT summary, created_at FROM summaries WHERE document_id = ?",
                (doc_id,)
            )
            row = cursor.fetchone()
            if row:
                return {
                    "summary": row["summary"],
                    "created_at": row["created_at"]
                }
            return None
    
    def save_summary(self, doc_id: str, summary: str) -> None:
        """Save a generated summary for a document"""
        with self.get_connection() as conn:
            conn.execute("""
                INSERT OR REPLACE INTO summaries (document_id, summary, created_at)
                VALUES (?, ?, CURRENT_TIMESTAMP)
            """, (doc_id, summary))
            conn.commit()
    
    def delete_summary(self, doc_id: str) -> bool:
        """Delete a cached summary (useful for regeneration)"""
        with self.get_connection() as conn:
            cursor = conn.execute(
                "DELETE FROM summaries WHERE document_id = ?",
                (doc_id,)
            )
            conn.commit()
            return cursor.rowcount > 0
    
    def get_summary_count(self) -> int:
        """Get total number of cached summaries"""
        with self.get_connection() as conn:
            cursor = conn.execute("SELECT COUNT(*) FROM summaries")
            return cursor.fetchone()[0]
    
    def insert_documents_batch(self, documents: List[Dict[str, Any]]):
        """Insert multiple documents efficiently in a single transaction"""
        if not documents:
            return
        
        # Prepare documents with extracted dates
        prepared_docs = []
        for doc in documents:
            doc_id = doc.get("id")
            if doc_id is None:
                continue  # skip docs without id (should not happen if build_index sets it)
            # Derive filename from path when missing (legacy JSON may only have path)
            path = doc.get("path", "")
            filename = doc.get("filename")
            if not filename and path:
                filename = Path(path).name
            if not filename:
                filename = "unknown"
            # Extract date from email headers if not already provided
            document_date = doc.get("document_date")
            if not document_date:
                full_text = doc.get("full_text", "")
                if full_text:
                    document_date = extract_email_date(full_text)
            
            prepared_docs.append((
                doc_id,
                filename,
                doc.get("original_filename", filename),
                path,
                doc.get("category", "Unknown"),
                doc.get("subcategory", ""),
                doc.get("file_type", "pdf"),
                doc.get("page_count", 0),
                doc.get("char_count", 0),
                doc.get("duration_seconds"),
                doc.get("full_text", ""),
                document_date
            ))
        
        with self.get_connection() as conn:
            conn.executemany("""
                INSERT OR REPLACE INTO documents 
                (id, filename, original_filename, path, category, subcategory, 
                 file_type, page_count, char_count, duration_seconds, full_text, document_date)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, prepared_docs)
            conn.commit()
    
    def get_indexed_doc_ids(self) -> set:
        """Get set of all document IDs currently in the database"""
        with self.get_connection() as conn:
            cursor = conn.execute("SELECT id FROM documents")
            return {row[0] for row in cursor.fetchall()}
    
    def get_indexed_paths(self) -> set:
        """Get set of all document paths currently in the database (for path-based deduplication)."""
        with self.get_connection() as conn:
            cursor = conn.execute("SELECT path FROM documents")
            return {row[0] for row in cursor.fetchall()}
    
    # =========================================================================
    # Settings Methods
    # =========================================================================
    
    def get_setting(self, key: str, default: str = None) -> Optional[str]:
        """Get a setting value by key"""
        with self.get_read_connection() as conn:
            cursor = conn.execute(
                "SELECT value FROM settings WHERE key = ?", (key,)
            )
            row = cursor.fetchone()
            return row["value"] if row else default
    
    def set_setting(self, key: str, value: str) -> None:
        """Set a setting value"""
        with self.get_connection() as conn:
            conn.execute("""
                INSERT OR REPLACE INTO settings (key, value, updated_at)
                VALUES (?, ?, CURRENT_TIMESTAMP)
            """, (key, value))
            conn.commit()
    
    def get_all_settings(self) -> Dict[str, str]:
        """Get all settings as a dictionary"""
        with self.get_read_connection() as conn:
            cursor = conn.execute("SELECT key, value FROM settings")
            return {row["key"]: row["value"] for row in cursor.fetchall()}
    
    # =========================================================================
    # Pinned Documents Methods
    # =========================================================================
    
    def get_pinned_documents(self, include_hidden: bool = False) -> List[Dict[str, Any]]:
        """Get all pinned documents with their details
        
        Args:
            include_hidden: If False (default), excludes hidden documents and hidden categories
        """
        with self.get_read_connection() as conn:
            if include_hidden:
                cursor = conn.execute("""
                    SELECT 
                        p.document_id, p.reason, p.display_order, p.pinned_at,
                        d.filename, d.path, d.category, d.subcategory, d.file_type,
                        d.page_count, d.char_count, d.is_hidden
                    FROM pinned_documents p
                    JOIN documents d ON p.document_id = d.id
                    ORDER BY p.display_order ASC, p.pinned_at DESC
                """)
            else:
                cursor = conn.execute("""
                    SELECT 
                        p.document_id, p.reason, p.display_order, p.pinned_at,
                        d.filename, d.path, d.category, d.subcategory, d.file_type,
                        d.page_count, d.char_count, d.is_hidden
                    FROM pinned_documents p
                    JOIN documents d ON p.document_id = d.id
                    LEFT JOIN hidden_categories hc ON d.category = hc.category
                    WHERE (d.is_hidden IS NULL OR d.is_hidden = 0)
                      AND hc.category IS NULL
                      AND d.subcategory != 'thumbnails'
                    ORDER BY p.display_order ASC, p.pinned_at DESC
                """)
            return [dict(row) for row in cursor.fetchall()]
    
    def pin_document(self, document_id: str, reason: str = None, display_order: int = 0) -> bool:
        """Pin a document with an optional reason"""
        with self.get_connection() as conn:
            # Verify document exists
            cursor = conn.execute("SELECT id FROM documents WHERE id = ?", (document_id,))
            if not cursor.fetchone():
                return False
            
            conn.execute("""
                INSERT OR REPLACE INTO pinned_documents (document_id, reason, display_order, pinned_at)
                VALUES (?, ?, ?, CURRENT_TIMESTAMP)
            """, (document_id, reason, display_order))
            conn.commit()
            return True
    
    def unpin_document(self, document_id: str) -> bool:
        """Unpin a document"""
        with self.get_connection() as conn:
            cursor = conn.execute(
                "DELETE FROM pinned_documents WHERE document_id = ?",
                (document_id,)
            )
            conn.commit()
            return cursor.rowcount > 0
    
    def update_pinned_document(self, document_id: str, reason: str = None, display_order: int = None) -> bool:
        """Update a pinned document's reason or order"""
        with self.get_connection() as conn:
            updates = []
            params = []
            
            if reason is not None:
                updates.append("reason = ?")
                params.append(reason)
            
            if display_order is not None:
                updates.append("display_order = ?")
                params.append(display_order)
            
            if not updates:
                return False
            
            params.append(document_id)
            cursor = conn.execute(
                f"UPDATE pinned_documents SET {', '.join(updates)} WHERE document_id = ?",
                params
            )
            conn.commit()
            return cursor.rowcount > 0
    
    def is_document_pinned(self, document_id: str) -> bool:
        """Check if a document is pinned"""
        with self.get_connection() as conn:
            cursor = conn.execute(
                "SELECT 1 FROM pinned_documents WHERE document_id = ?",
                (document_id,)
            )
            return cursor.fetchone() is not None
    
    # =========================================================================
    # Keywords Methods
    # =========================================================================
    
    def get_keywords(self, active_only: bool = True) -> List[Dict[str, Any]]:
        """Get all keywords, optionally filtered to active only
        
        Args:
            active_only: If True, only return keywords with is_active=1
        
        Returns:
            List of keyword dictionaries
        """
        with self.get_read_connection() as conn:
            if active_only:
                cursor = conn.execute("""
                    SELECT id, name, search_term, category, document_count, display_order, is_active, created_at
                    FROM keywords
                    WHERE is_active = 1
                    ORDER BY category, display_order, name
                """)
            else:
                cursor = conn.execute("""
                    SELECT id, name, search_term, category, document_count, display_order, is_active, created_at
                    FROM keywords
                    ORDER BY category, display_order, name
                """)
            return [dict(row) for row in cursor.fetchall()]
    
    def get_keyword(self, keyword_id: int) -> Optional[Dict[str, Any]]:
        """Get a single keyword by ID"""
        with self.get_connection() as conn:
            cursor = conn.execute(
                "SELECT * FROM keywords WHERE id = ?", (keyword_id,)
            )
            row = cursor.fetchone()
            return dict(row) if row else None
    
    def add_keyword(self, name: str, search_term: str, category: str, 
                    display_order: int = 0, is_active: bool = True) -> Optional[int]:
        """Add a new keyword
        
        Args:
            name: Display name (e.g., "Ghislaine Maxwell")
            search_term: Search term (e.g., "Maxwell")
            category: Category (e.g., "People", "Locations", "Topics")
            display_order: Order in dropdown
            is_active: Whether to show in public dropdown
        
        Returns:
            ID of the new keyword, or None if failed (duplicate name)
        """
        with self.get_connection() as conn:
            try:
                cursor = conn.execute("""
                    INSERT INTO keywords (name, search_term, category, display_order, is_active)
                    VALUES (?, ?, ?, ?, ?)
                """, (name, search_term, category, display_order, 1 if is_active else 0))
                conn.commit()
                return cursor.lastrowid
            except sqlite3.IntegrityError:
                # Duplicate name
                return None
    
    def update_keyword(self, keyword_id: int, name: str = None, search_term: str = None,
                       category: str = None, display_order: int = None, 
                       is_active: bool = None) -> bool:
        """Update an existing keyword
        
        Returns:
            True if updated, False if keyword not found
        """
        with self.get_connection() as conn:
            updates = []
            params = []
            
            if name is not None:
                updates.append("name = ?")
                params.append(name)
            
            if search_term is not None:
                updates.append("search_term = ?")
                params.append(search_term)
            
            if category is not None:
                updates.append("category = ?")
                params.append(category)
            
            if display_order is not None:
                updates.append("display_order = ?")
                params.append(display_order)
            
            if is_active is not None:
                updates.append("is_active = ?")
                params.append(1 if is_active else 0)
            
            if not updates:
                return False
            
            params.append(keyword_id)
            try:
                cursor = conn.execute(
                    f"UPDATE keywords SET {', '.join(updates)} WHERE id = ?",
                    params
                )
                conn.commit()
                return cursor.rowcount > 0
            except sqlite3.IntegrityError:
                # Duplicate name
                return False
    
    def delete_keyword(self, keyword_id: int) -> bool:
        """Delete a keyword
        
        Returns:
            True if deleted, False if not found
        """
        with self.get_connection() as conn:
            cursor = conn.execute(
                "DELETE FROM keywords WHERE id = ?", (keyword_id,)
            )
            conn.commit()
            return cursor.rowcount > 0
    
    def update_keyword_counts(self) -> Dict[str, int]:
        """Recount document matches for all keywords
        
        Scans the documents table full_text for each keyword's search_term.
        
        Returns:
            Dictionary mapping keyword names to their new counts
        """
        with self.get_connection() as conn:
            # Get all keywords
            cursor = conn.execute("SELECT id, name, search_term FROM keywords")
            keywords = cursor.fetchall()
            
            results = {}
            for kw in keywords:
                keyword_id = kw[0]
                name = kw[1]
                search_term = kw[2]
                
                # Count documents containing this search term (case-insensitive)
                cursor = conn.execute("""
                    SELECT COUNT(*) FROM documents 
                    WHERE LOWER(full_text) LIKE LOWER(?)
                """, (f"%{search_term}%",))
                count = cursor.fetchone()[0]
                
                # Update the keyword's document_count
                conn.execute(
                    "UPDATE keywords SET document_count = ? WHERE id = ?",
                    (count, keyword_id)
                )
                results[name] = count
            
            conn.commit()
            return results
    
    def seed_default_keywords(self) -> int:
        """Seed the database with default keywords if empty
        
        Returns:
            Number of keywords added
        """
        with self.get_connection() as conn:
            # Check if keywords already exist
            cursor = conn.execute("SELECT COUNT(*) FROM keywords")
            if cursor.fetchone()[0] > 0:
                return 0
            
            # Default keywords matching the existing hardcoded ones
            default_keywords = [
                # People
                ("Ghislaine Maxwell", "Maxwell", "People", 1),
                ("Bill Clinton", "Clinton", "People", 2),
                ("Donald Trump", "Trump", "People", 3),
                ("Prince Andrew", "Andrew", "People", 4),
                ("Alan Dershowitz", "Dershowitz", "People", 5),
                ("Jean-Luc Brunel", "Brunel", "People", 6),
                ("Virginia Giuffre", "Giuffre", "People", 7),
                ("Sarah Kellen", "Kellen", "People", 8),
                ("Les Wexner", "Wexner", "People", 9),
                ("Leon Black", "Black", "People", 10),
                
                # Locations
                ("Palm Beach", "Palm Beach", "Locations", 1),
                ("Manhattan / New York", "Manhattan", "Locations", 2),
                ("Little St. James Island", "Little St. James", "Locations", 3),
                ("Zorro Ranch", "Zorro Ranch", "Locations", 4),
                ("Paris", "Paris", "Locations", 5),
                
                # Topics
                ("Flight Logs", "flight", "Topics", 1),
                ("Massage", "massage", "Topics", 2),
                ("Trafficking", "trafficking", "Topics", 3),
                ("Minors / Underage", "minor", "Topics", 4),
                ("Victims", "victim", "Topics", 5),
                ("Settlement", "settlement", "Topics", 6),
                ("Deposition", "deposition", "Topics", 7),
                ("Interview", "interview", "Topics", 8),
                ("FBI", "FBI", "Topics", 9),
                ("DOJ", "DOJ", "Topics", 10),
            ]
            
            conn.executemany("""
                INSERT INTO keywords (name, search_term, category, display_order, is_active)
                VALUES (?, ?, ?, ?, 1)
            """, default_keywords)
            conn.commit()
            
            return len(default_keywords)
    
    # =========================================================================
    # Missing Documents Methods (404 Tracking)
    # =========================================================================
    
    def add_missing_document(self, filename: str, url: str, dataset_num: int, 
                             page_found_on: int = None) -> bool:
        """Add or update a missing (404) document
        
        Args:
            filename: The filename (e.g., "EFTA00123456.pdf")
            url: Full URL to the file
            dataset_num: Dataset number (9, 10, 11, etc.)
            page_found_on: Page number where the link was found
        
        Returns:
            True if added/updated successfully
        """
        with self.get_connection() as conn:
            try:
                # Try to insert, or update if exists
                cursor = conn.execute("""
                    INSERT INTO missing_documents (filename, url, dataset_num, page_found_on)
                    VALUES (?, ?, ?, ?)
                    ON CONFLICT(filename, dataset_num) DO UPDATE SET
                        last_checked = CURRENT_TIMESTAMP,
                        check_count = check_count + 1
                """, (filename, url, dataset_num, page_found_on))
                conn.commit()
                return True
            except Exception as e:
                print(f"Error adding missing document: {e}")
                return False
    
    def get_missing_documents(self, dataset_num: int = None) -> List[Dict[str, Any]]:
        """Get all missing documents, optionally filtered by dataset
        
        Args:
            dataset_num: If provided, filter to this dataset only
        
        Returns:
            List of missing document dictionaries
        """
        with self.get_connection() as conn:
            if dataset_num is not None:
                cursor = conn.execute("""
                    SELECT id, filename, url, dataset_num, page_found_on, 
                           first_seen, last_checked, check_count
                    FROM missing_documents
                    WHERE dataset_num = ?
                    ORDER BY filename
                """, (dataset_num,))
            else:
                cursor = conn.execute("""
                    SELECT id, filename, url, dataset_num, page_found_on,
                           first_seen, last_checked, check_count
                    FROM missing_documents
                    ORDER BY dataset_num, filename
                """)
            return [dict(row) for row in cursor.fetchall()]
    
    def remove_missing_document(self, filename: str, dataset_num: int) -> bool:
        """Remove a document from missing list (e.g., if it becomes available)
        
        Returns:
            True if removed, False if not found
        """
        with self.get_connection() as conn:
            cursor = conn.execute(
                "DELETE FROM missing_documents WHERE filename = ? AND dataset_num = ?",
                (filename, dataset_num)
            )
            conn.commit()
            return cursor.rowcount > 0
    
    def get_missing_documents_stats(self, timeout_seconds: float = 0) -> Dict[str, Any]:
        """Get statistics on missing documents

        Returns:
            Dictionary with total count and counts by dataset
        """
        with self.get_read_connection(timeout_seconds=timeout_seconds) as conn:
            # Total count
            cursor = conn.execute("SELECT COUNT(*) FROM missing_documents")
            total = cursor.fetchone()[0]
            
            # By dataset
            cursor = conn.execute("""
                SELECT dataset_num, COUNT(*) as count
                FROM missing_documents
                GROUP BY dataset_num
                ORDER BY dataset_num
            """)
            by_dataset = [{"dataset_num": row[0], "count": row[1]} for row in cursor.fetchall()]
            
            return {
                "total": total,
                "by_dataset": by_dataset
            }
    
    # =========================================================================
    # DOJ Manifest Methods (Completeness Tracking)
    # =========================================================================
    
    def add_to_manifest(self, filename: str, url: str, dataset_num: int,
                        page_found_on: int = None, status: str = 'found') -> bool:
        """Add a file to the DOJ manifest
        
        Args:
            filename: The filename (e.g., "EFTA00123456.pdf")
            url: Full URL to the file
            dataset_num: Dataset number
            page_found_on: Page number where the link was found
            status: Status - 'found', 'downloaded', '404', 'failed'
        
        Returns:
            True if added successfully
        """
        with self.get_connection() as conn:
            try:
                cursor = conn.execute("""
                    INSERT INTO doj_manifest (filename, url, dataset_num, page_found_on, status)
                    VALUES (?, ?, ?, ?, ?)
                    ON CONFLICT(filename, dataset_num) DO UPDATE SET
                        last_updated = CURRENT_TIMESTAMP
                """, (filename, url, dataset_num, page_found_on, status))
                conn.commit()
                return True
            except Exception as e:
                print(f"Error adding to manifest: {e}")
                return False
    
    def update_manifest_status(self, filename: str, dataset_num: int, status: str) -> bool:
        """Update the status of a file in the manifest
        
        Args:
            filename: The filename
            dataset_num: Dataset number
            status: New status - 'found', 'downloaded', '404', 'failed'
        
        Returns:
            True if updated, False if not found
        """
        with self.get_connection() as conn:
            cursor = conn.execute("""
                UPDATE doj_manifest 
                SET status = ?, last_updated = CURRENT_TIMESTAMP
                WHERE filename = ? AND dataset_num = ?
            """, (status, filename, dataset_num))
            conn.commit()
            return cursor.rowcount > 0
    
    def get_manifest(self, dataset_num: int = None, status: str = None) -> List[Dict[str, Any]]:
        """Get manifest entries
        
        Args:
            dataset_num: Filter by dataset number
            status: Filter by status
        
        Returns:
            List of manifest entries
        """
        with self.get_connection() as conn:
            conditions = []
            params = []
            
            if dataset_num is not None:
                conditions.append("dataset_num = ?")
                params.append(dataset_num)
            
            if status is not None:
                conditions.append("status = ?")
                params.append(status)
            
            sql = """
                SELECT id, filename, url, dataset_num, page_found_on, status,
                       first_seen, last_updated
                FROM doj_manifest
            """
            
            if conditions:
                sql += " WHERE " + " AND ".join(conditions)
            
            sql += " ORDER BY dataset_num, filename"
            
            cursor = conn.execute(sql, params)
            return [dict(row) for row in cursor.fetchall()]
    
    def get_manifest_stats(self, timeout_seconds: float = 0) -> Dict[str, Any]:
        """Get completeness statistics from manifest

        Returns:
            Dictionary with total and per-dataset status counts
        """
        with self.get_read_connection(timeout_seconds=timeout_seconds) as conn:
            # Overall counts by status
            cursor = conn.execute("""
                SELECT status, COUNT(*) as count
                FROM doj_manifest
                GROUP BY status
            """)
            overall = {row[0]: row[1] for row in cursor.fetchall()}
            
            # Per-dataset counts
            cursor = conn.execute("""
                SELECT dataset_num, status, COUNT(*) as count
                FROM doj_manifest
                GROUP BY dataset_num, status
                ORDER BY dataset_num
            """)
            
            by_dataset = {}
            for row in cursor.fetchall():
                dataset = row[0]
                status = row[1]
                count = row[2]
                
                if dataset not in by_dataset:
                    by_dataset[dataset] = {'total': 0, 'downloaded': 0, '404': 0, 'failed': 0, 'found': 0}
                
                by_dataset[dataset][status] = count
                by_dataset[dataset]['total'] += count
            
            # Total across all
            cursor = conn.execute("SELECT COUNT(*) FROM doj_manifest")
            total = cursor.fetchone()[0]
            
            return {
                "total": total,
                "overall": overall,
                "by_dataset": by_dataset
            }

    def get_dataset_db_counts(self, timeout_seconds: float = 0) -> Dict[str, Any]:
        """Per-dataset counts of documents ACTUALLY present in the DB, bucketed
        by EFTA number into the 12 DOJ datasets via DATASET_EFTA_RANGES.

        This is the authoritative "what we hold" view (unlike the doj_manifest
        scrape-tracker, which is stale/partial). EFTA filenames are
        'EFTA' + 8 zero-padded digits (e.g. EFTA00549159.pdf); a timestamp-
        suffixed re-download like EFTA00039190_20260130_203030.pdf still parses
        because we read the 8 digits at positions 5-12.
        """
        ranges = {
            1: (1, 3158), 2: (3159, 3857), 3: (3858, 5704), 4: (5705, 8408),
            5: (8409, 8528), 6: (8529, 9015), 7: (9016, 9675), 8: (9676, 39024),
            9: (39025, 1262781), 10: (1262782, 2212882), 11: (2212883, 2730264),
            12: (2730265, 3000000),
        }
        case_sql = " ".join(
            f"WHEN n BETWEEN {a} AND {b} THEN {ds}" for ds, (a, b) in ranges.items()
        )
        sql = f"""
            SELECT CASE {case_sql} ELSE 0 END AS ds, COUNT(*) AS cnt
            FROM (SELECT CAST(SUBSTR(filename, 5, 8) AS INTEGER) AS n
                  FROM documents WHERE filename LIKE 'EFTA%')
            GROUP BY ds
        """
        with self.get_read_connection(timeout_seconds=timeout_seconds) as conn:
            by_dataset = {str(d): 0 for d in range(1, 13)}
            unranged = 0
            for ds, cnt in conn.execute(sql):
                if ds and 1 <= ds <= 12:
                    by_dataset[str(ds)] = cnt
                else:
                    unranged += cnt
            non_efta = conn.execute(
                "SELECT COUNT(*) FROM documents WHERE filename NOT LIKE 'EFTA%'"
            ).fetchone()[0]
            total = conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0]
        return {
            "by_dataset": by_dataset,
            "non_efta": non_efta,
            "unranged": unranged,
            "total": total,
        }

    def get_not_downloaded(self, dataset_num: int = None) -> List[Dict[str, Any]]:
        """Get files that are in manifest but not successfully downloaded
        
        Args:
            dataset_num: Filter by dataset number
        
        Returns:
            List of files with status != 'downloaded'
        """
        with self.get_connection() as conn:
            if dataset_num is not None:
                cursor = conn.execute("""
                    SELECT id, filename, url, dataset_num, page_found_on, status,
                           first_seen, last_updated
                    FROM doj_manifest
                    WHERE dataset_num = ? AND status != 'downloaded'
                    ORDER BY filename
                """, (dataset_num,))
            else:
                cursor = conn.execute("""
                    SELECT id, filename, url, dataset_num, page_found_on, status,
                           first_seen, last_updated
                    FROM doj_manifest
                    WHERE status != 'downloaded'
                    ORDER BY dataset_num, filename
                """)
            return [dict(row) for row in cursor.fetchall()]
    
    def bulk_add_to_manifest(self, entries: List[Dict[str, Any]]) -> int:
        """Bulk add entries to the manifest (more efficient for batch operations)
        
        Args:
            entries: List of dicts with keys: filename, url, dataset_num, page_found_on, status
        
        Returns:
            Number of entries added
        """
        if not entries:
            return 0
        
        with self.get_connection() as conn:
            cursor = conn.executemany("""
                INSERT INTO doj_manifest (filename, url, dataset_num, page_found_on, status)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(filename, dataset_num) DO UPDATE SET
                    last_updated = CURRENT_TIMESTAMP
            """, [(e['filename'], e['url'], e['dataset_num'], 
                   e.get('page_found_on'), e.get('status', 'found')) for e in entries])
            conn.commit()
            return len(entries)
    
    def clear_manifest(self, dataset_num: int = None) -> int:
        """Clear manifest entries (use with caution)
        
        Args:
            dataset_num: If provided, only clear this dataset
        
        Returns:
            Number of entries deleted
        """
        with self.get_connection() as conn:
            if dataset_num is not None:
                cursor = conn.execute(
                    "DELETE FROM doj_manifest WHERE dataset_num = ?",
                    (dataset_num,)
                )
            else:
                cursor = conn.execute("DELETE FROM doj_manifest")
            conn.commit()
            return cursor.rowcount
    
    def get_documents_for_export(self, category: Optional[str] = None,
                                  subcategory: Optional[str] = None,
                                  file_type: Optional[str] = None,
                                  filename: Optional[str] = None,
                                  keyword: Optional[str] = None,
                                  search_query: Optional[str] = None,
                                  include_text: bool = False,
                                  max_results: int = 50000) -> List[Dict[str, Any]]:
        """Get documents with DOJ manifest URLs for CSV export
        
        Args:
            category: Filter by category
            subcategory: Filter by subcategory
            file_type: Filter by file type
            filename: Partial filename match
            keyword: Keyword search using FTS
            search_query: Full-text search query
            include_text: Include full_text in results (capped at 5,000 rows)
            max_results: Maximum number of results (default 50,000)
        
        Returns:
            List of dicts with: filename, category, subcategory, file_type,
            page_count, char_count, document_date, doj_url, and optionally full_text
        """
        if include_text:
            max_results = min(max_results, 5000)

        text_col = ", d.full_text" if include_text else ""

        with self.get_read_connection() as conn:
            params = []
            conditions = []
            
            # Base query with LEFT JOIN to doj_manifest
            if search_query:
                # Full-text search mode
                sql = f"""
                    SELECT DISTINCT 
                        d.filename, d.category, d.subcategory,
                        d.file_type, d.page_count, d.char_count,
                        d.document_date, dm.url as doj_url{text_col}
                    FROM documents_fts
                    JOIN documents d ON documents_fts.id = d.id
                    LEFT JOIN hidden_categories hc ON d.category = hc.category
                    LEFT JOIN doj_manifest dm ON d.filename = dm.filename
                    WHERE documents_fts MATCH ?
                """
                params.append(search_query)
                # Exclude hidden
                conditions.append("(d.is_hidden IS NULL OR d.is_hidden = 0)")
                conditions.append("hc.category IS NULL")
                conditions.append("d.subcategory != 'thumbnails'")
            elif keyword:
                # Keyword search mode
                sql = f"""
                    SELECT DISTINCT 
                        d.filename, d.category, d.subcategory,
                        d.file_type, d.page_count, d.char_count,
                        d.document_date, dm.url as doj_url{text_col}
                    FROM documents d
                    JOIN documents_fts fts ON d.id = fts.id
                    LEFT JOIN hidden_categories hc ON d.category = hc.category
                    LEFT JOIN doj_manifest dm ON d.filename = dm.filename
                    WHERE documents_fts MATCH ?
                """
                escaped_keyword = keyword.replace('"', '""')
                params.append(f'"{escaped_keyword}"*')
                # Exclude hidden
                conditions.append("(d.is_hidden IS NULL OR d.is_hidden = 0)")
                conditions.append("hc.category IS NULL")
                conditions.append("d.subcategory != 'thumbnails'")
            else:
                # Browse mode (no search)
                sql = f"""
                    SELECT 
                        d.filename, d.category, d.subcategory,
                        d.file_type, d.page_count, d.char_count,
                        d.document_date, dm.url as doj_url{text_col}
                    FROM documents d
                    LEFT JOIN hidden_categories hc ON d.category = hc.category
                    LEFT JOIN doj_manifest dm ON d.filename = dm.filename
                """
                # Exclude hidden
                conditions.append("(d.is_hidden IS NULL OR d.is_hidden = 0)")
                conditions.append("hc.category IS NULL")
                conditions.append("d.subcategory != 'thumbnails'")
            
            # Apply filters
            if category:
                conditions.append("d.category = ?")
                params.append(category)
            
            if subcategory:
                conditions.append("d.subcategory = ?")
                params.append(subcategory)
            
            if file_type:
                conditions.append("d.file_type = ?")
                params.append(file_type)
            
            if filename:
                conditions.append("LOWER(d.filename) LIKE LOWER(?)")
                params.append(f"%{filename}%")
            
            # Add conditions to query
            if conditions:
                if search_query or keyword:
                    sql += " AND " + " AND ".join(conditions)
                else:
                    sql += " WHERE " + " AND ".join(conditions)
            
            sql += " ORDER BY d.filename LIMIT ?"
            params.append(max_results)
            
            cursor = conn.execute(sql, params)
            results = [dict(row) for row in cursor]
            
            # Generate DOJ URLs for documents missing them
            for doc in results:
                if not doc.get('doj_url') and doc.get('category') == 'DOJ Disclosures':
                    subcategory = doc.get('subcategory', '')
                    # Extract dataset number from "Data Set N"
                    match = re.match(r'Data Set (\d+)', subcategory)
                    if match:
                        dataset_num = match.group(1)
                        filename = doc.get('filename', '')
                        doc['doj_url'] = f"https://www.justice.gov/epstein/files/DataSet%20{dataset_num}/{filename}"
            
            return results
    
    # =========================================================================
    # Document Visibility Methods
    # =========================================================================
    
    def hide_document(self, doc_id: str) -> bool:
        """Hide a document from public view
        
        Returns:
            True if updated, False if document not found
        """
        with self.get_connection() as conn:
            cursor = conn.execute(
                "UPDATE documents SET is_hidden = 1 WHERE id = ?",
                (doc_id,)
            )
            conn.commit()
            return cursor.rowcount > 0
    
    def unhide_document(self, doc_id: str) -> bool:
        """Unhide a document (make visible to public)
        
        Returns:
            True if updated, False if document not found
        """
        with self.get_connection() as conn:
            cursor = conn.execute(
                "UPDATE documents SET is_hidden = 0 WHERE id = ?",
                (doc_id,)
            )
            conn.commit()
            return cursor.rowcount > 0
    
    def is_document_hidden(self, doc_id: str) -> bool:
        """Check if a specific document is hidden"""
        with self.get_read_connection() as conn:
            cursor = conn.execute(
                "SELECT is_hidden FROM documents WHERE id = ?",
                (doc_id,)
            )
            row = cursor.fetchone()
            return row and row[0] == 1
    
    def get_hidden_documents(self, limit: int = 100, offset: int = 0, timeout_seconds: float = 0) -> List[Dict[str, Any]]:
        """Get all hidden documents (for admin panel)"""
        with self.get_read_connection(timeout_seconds=timeout_seconds) as conn:
            cursor = conn.execute("""
                SELECT id, filename, path, category, subcategory, file_type, 
                       page_count, char_count, is_hidden
                FROM documents 
                WHERE is_hidden = 1
                ORDER BY filename
                LIMIT ? OFFSET ?
            """, (limit, offset))
            return [dict(row) for row in cursor.fetchall()]
    
    def count_hidden_documents(self, timeout_seconds: float = 0) -> int:
        """Count total hidden documents"""
        with self.get_read_connection(timeout_seconds=timeout_seconds) as conn:
            cursor = conn.execute("SELECT COUNT(*) FROM documents WHERE is_hidden = 1")
            return cursor.fetchone()[0]
    
    def bulk_hide_documents(self, doc_ids: List[str]) -> int:
        """Hide multiple documents from public view in a single query
        
        Args:
            doc_ids: List of document IDs to hide (max 1000)
        
        Returns:
            Number of documents actually updated
        """
        if not doc_ids:
            return 0
        doc_ids = doc_ids[:1000]  # Cap at 1000 for safety
        with self.get_connection() as conn:
            placeholders = ','.join('?' * len(doc_ids))
            cursor = conn.execute(
                f"UPDATE documents SET is_hidden = 1 WHERE id IN ({placeholders}) AND is_hidden = 0",
                doc_ids
            )
            conn.commit()
            return cursor.rowcount
    
    def bulk_unhide_documents(self, doc_ids: List[str]) -> int:
        """Unhide multiple documents in a single query
        
        Args:
            doc_ids: List of document IDs to unhide (max 1000)
        
        Returns:
            Number of documents actually updated
        """
        if not doc_ids:
            return 0
        doc_ids = doc_ids[:1000]  # Cap at 1000 for safety
        with self.get_connection() as conn:
            placeholders = ','.join('?' * len(doc_ids))
            cursor = conn.execute(
                f"UPDATE documents SET is_hidden = 0 WHERE id IN ({placeholders}) AND is_hidden = 1",
                doc_ids
            )
            conn.commit()
            return cursor.rowcount
    
    def hide_documents_by_filename_pattern(self, pattern: str) -> int:
        """Hide documents matching a filename pattern (SQL LIKE)
        
        Args:
            pattern: SQL LIKE pattern (e.g., 'EFTA016883%')
        
        Returns:
            Number of documents hidden
        """
        if not pattern or not pattern.strip():
            return 0
        with self.get_connection() as conn:
            cursor = conn.execute(
                "UPDATE documents SET is_hidden = 1 WHERE filename LIKE ? AND is_hidden = 0",
                (pattern,)
            )
            conn.commit()
            return cursor.rowcount
    
    def bulk_hide_by_filenames(self, filenames: List[str]) -> Dict[str, Any]:
        """Hide documents by exact filename match (for CSV/bulk paste operations)
        
        Args:
            filenames: List of filenames to hide (max 5000)
        
        Returns:
            Dict with hidden_count, not_found filenames, and already_hidden filenames
        """
        if not filenames:
            return {"hidden_count": 0, "not_found": [], "already_hidden": []}
        filenames = filenames[:5000]  # Cap for safety
        with self.get_connection() as conn:
            not_found = []
            already_hidden = []
            to_hide_ids = []
            
            for fname in filenames:
                fname = fname.strip()
                if not fname:
                    continue
                cursor = conn.execute(
                    "SELECT id, is_hidden FROM documents WHERE filename = ?",
                    (fname,)
                )
                row = cursor.fetchone()
                if not row:
                    not_found.append(fname)
                elif row["is_hidden"] == 1:
                    already_hidden.append(fname)
                else:
                    to_hide_ids.append(row["id"])
            
            hidden_count = 0
            if to_hide_ids:
                placeholders = ','.join('?' * len(to_hide_ids))
                cursor = conn.execute(
                    f"UPDATE documents SET is_hidden = 1 WHERE id IN ({placeholders})",
                    to_hide_ids
                )
                conn.commit()
                hidden_count = cursor.rowcount
            
            return {
                "hidden_count": hidden_count,
                "not_found": not_found,
                "already_hidden": already_hidden
            }
    
    def bulk_unhide_by_filenames(self, filenames: List[str]) -> Dict[str, Any]:
        """Unhide documents by exact filename match (for CSV/bulk paste operations)
        
        Args:
            filenames: List of filenames to unhide (max 5000)
        
        Returns:
            Dict with unhidden_count, not_found filenames, and already_visible filenames
        """
        if not filenames:
            return {"unhidden_count": 0, "not_found": [], "already_visible": []}
        filenames = filenames[:5000]
        with self.get_connection() as conn:
            not_found = []
            already_visible = []
            to_unhide_ids = []
            
            for fname in filenames:
                fname = fname.strip()
                if not fname:
                    continue
                cursor = conn.execute(
                    "SELECT id, is_hidden FROM documents WHERE filename = ?",
                    (fname,)
                )
                row = cursor.fetchone()
                if not row:
                    not_found.append(fname)
                elif row["is_hidden"] != 1:
                    already_visible.append(fname)
                else:
                    to_unhide_ids.append(row["id"])
            
            unhidden_count = 0
            if to_unhide_ids:
                placeholders = ','.join('?' * len(to_unhide_ids))
                cursor = conn.execute(
                    f"UPDATE documents SET is_hidden = 0 WHERE id IN ({placeholders})",
                    to_unhide_ids
                )
                conn.commit()
                unhidden_count = cursor.rowcount
            
            return {
                "unhidden_count": unhidden_count,
                "not_found": not_found,
                "already_visible": already_visible
            }
    
    VALID_FILE_TYPES = {"pdf", "document", "image", "audio", "video"}

    def update_file_type(self, document_ids: List[str], file_type: str) -> int:
        """Reclassify documents to a new file_type.

        Args:
            document_ids: List of document IDs to update (max 1000)
            file_type: Target file_type (must be in VALID_FILE_TYPES)

        Returns:
            Number of rows actually updated
        """
        if not document_ids:
            return 0
        if file_type not in self.VALID_FILE_TYPES:
            raise ValueError(f"Invalid file_type: {file_type}")
        document_ids = document_ids[:1000]
        with self.get_connection() as conn:
            placeholders = ','.join('?' * len(document_ids))
            cursor = conn.execute(
                f"UPDATE documents SET file_type = ? WHERE id IN ({placeholders})",
                [file_type] + document_ids
            )
            conn.commit()
            return cursor.rowcount

    def get_documents_by_filenames(self, filenames: List[str], include_hidden: bool = True) -> Dict[str, Dict[str, Any]]:
        """Resolve a list of filenames to document records in bulk.

        Args:
            filenames: List of filenames to look up (max 500)
            include_hidden: Whether to include hidden documents

        Returns:
            Dict mapping filename -> document record for found documents
        """
        if not filenames:
            return {}
        filenames = filenames[:500]
        result = {}
        with self.get_connection() as conn:
            hidden_clause = "" if include_hidden else " AND is_hidden = 0"
            for fname in filenames:
                fname = fname.strip()
                if not fname:
                    continue
                cursor = conn.execute(
                    f"SELECT id, filename, original_filename, path, category, subcategory, "
                    f"file_type, page_count, char_count, is_hidden FROM documents "
                    f"WHERE filename = ?{hidden_clause}",
                    (fname,)
                )
                row = cursor.fetchone()
                if row:
                    result[fname] = dict(row)
        return result

    # =========================================================================
    # Category Visibility Methods
    # =========================================================================
    
    def hide_category(self, category: str) -> bool:
        """Hide an entire category from public view
        
        Returns:
            True if added/updated
        """
        with self.get_connection() as conn:
            try:
                conn.execute("""
                    INSERT OR REPLACE INTO hidden_categories (category, hidden_at)
                    VALUES (?, CURRENT_TIMESTAMP)
                """, (category,))
                conn.commit()
                return True
            except Exception:
                return False
    
    def unhide_category(self, category: str) -> bool:
        """Unhide a category (make visible to public)
        
        Returns:
            True if removed, False if not found
        """
        with self.get_connection() as conn:
            cursor = conn.execute(
                "DELETE FROM hidden_categories WHERE category = ?",
                (category,)
            )
            conn.commit()
            return cursor.rowcount > 0
    
    def is_category_hidden(self, category: str) -> bool:
        """Check if a category is hidden"""
        with self.get_read_connection() as conn:
            cursor = conn.execute(
                "SELECT 1 FROM hidden_categories WHERE category = ?",
                (category,)
            )
            return cursor.fetchone() is not None
    
    def get_hidden_categories(self) -> List[Dict[str, Any]]:
        """Get all hidden categories with document counts"""
        with self.get_connection() as conn:
            cursor = conn.execute("""
                SELECT 
                    hc.category,
                    hc.hidden_at,
                    COUNT(d.id) as document_count
                FROM hidden_categories hc
                LEFT JOIN documents d ON d.category = hc.category
                GROUP BY hc.category
                ORDER BY hc.category
            """)
            return [dict(row) for row in cursor.fetchall()]
    
    def is_document_visible(self, doc_id: str) -> bool:
        """Check if a document is visible to public (not hidden AND category not hidden)
        
        This is the main visibility check that should be used by all public endpoints.
        
        Returns:
            True if document exists and is visible, False otherwise
        """
        with self.get_read_connection() as conn:
            cursor = conn.execute("""
                SELECT d.id 
                FROM documents d
                LEFT JOIN hidden_categories hc ON d.category = hc.category
                WHERE d.id = ? 
                  AND (d.is_hidden IS NULL OR d.is_hidden = 0)
                  AND hc.category IS NULL
            """, (doc_id,))
            return cursor.fetchone() is not None
    
    def get_all_categories_with_visibility(self, timeout_seconds: float = 0) -> List[Dict[str, Any]]:
        """Get all categories with their visibility status and document counts (for admin).
        Uses a read connection (was incorrectly on the write lock) with an optional timeout."""
        with self.get_read_connection(timeout_seconds=timeout_seconds) as conn:
            cursor = conn.execute("""
                SELECT 
                    d.category,
                    COUNT(*) as document_count,
                    CASE WHEN hc.category IS NOT NULL THEN 1 ELSE 0 END as is_hidden
                FROM documents d
                LEFT JOIN hidden_categories hc ON d.category = hc.category
                GROUP BY d.category
                ORDER BY d.category
            """)
            return [dict(row) for row in cursor.fetchall()]


class VectorStore:
    """Simple vector store using sentence-transformers and numpy"""
    
    def __init__(self, persist_dir: str):
        self.persist_dir = Path(persist_dir)
        self.persist_dir.mkdir(parents=True, exist_ok=True)
        
        self.embeddings_file = self.persist_dir / "embeddings.pkl"
        self.metadata_file = self.persist_dir / "metadata.pkl"
        
        self.embeddings = []
        self.metadata = []
        self.model = None
        self._embeddings_matrix = None
        self._embeddings_norms = None
        self._embedding_count = 0
        
        self._load()
    
    def _load(self):
        """Load metadata from disk; embeddings are loaded lazily on first search."""
        if self.embeddings_file.exists() and self.metadata_file.exists():
            try:
                with open(self.metadata_file, 'rb') as f:
                    self.metadata = pickle.load(f)
                self._embedding_count = len(self.metadata)
                print(f"Loaded {self._embedding_count} embeddings metadata from disk (matrix deferred)")
            except Exception as e:
                print(f"Error loading metadata: {e}")
                self.metadata = []
                self._embedding_count = 0

    def _ensure_matrix(self):
        """Lazy-load the embeddings matrix on first use."""
        if self._embeddings_matrix is not None:
            return True
        if not self.embeddings_file.exists():
            return False
        try:
            with open(self.embeddings_file, 'rb') as f:
                raw = pickle.load(f)
            if raw:
                self._embeddings_matrix = np.array(raw, dtype=np.float32)
                self._embeddings_norms = np.linalg.norm(self._embeddings_matrix, axis=1)
                self._embedding_count = self._embeddings_matrix.shape[0]
            del raw
            print(f"Loaded embeddings matrix ({self._embedding_count} vectors)")
            return self._embeddings_matrix is not None
        except Exception as e:
            print(f"Error loading embeddings matrix: {e}")
            return False
    
    def _save(self):
        """Save embeddings to disk"""
        embs_to_save = self.embeddings if self.embeddings else (
            [row for row in self._embeddings_matrix] if self._embeddings_matrix is not None else []
        )
        with open(self.embeddings_file, 'wb') as f:
            pickle.dump(embs_to_save, f)
        with open(self.metadata_file, 'wb') as f:
            pickle.dump(self.metadata, f)
        if self.embeddings:
            self._embeddings_matrix = np.array(self.embeddings, dtype=np.float32)
            self._embeddings_norms = np.linalg.norm(self._embeddings_matrix, axis=1)
        elif self._embeddings_matrix is not None:
            self._embeddings_norms = np.linalg.norm(self._embeddings_matrix, axis=1)
    
    def _get_model(self):
        """Lazy load the embedding model with GPU support"""
        if self.model is None:
            try:
                from sentence_transformers import SentenceTransformer
                import torch
                
                # Detect best available device
                if torch.backends.mps.is_available():
                    device = "mps"  # Apple Silicon GPU
                elif torch.cuda.is_available():
                    device = "cuda"  # NVIDIA GPU
                else:
                    device = "cpu"
                
                print(f"  Loading embedding model (device={device})...")
                self.model = SentenceTransformer('all-MiniLM-L6-v2', device=device)
            except ImportError:
                print("sentence-transformers not installed, semantic search disabled")
                return None
        return self.model
    
    def get_indexed_doc_ids(self) -> set:
        """Get set of already indexed document IDs"""
        return {m.get("doc_id") for m in self.metadata if m.get("doc_id")}
    
    def remove_doc_ids(self, doc_ids: set) -> int:
        """Remove embeddings for the given document IDs. Returns number removed."""
        if not doc_ids:
            return 0
        kept_embeddings = []
        kept_metadata = []
        removed = 0
        for i, m in enumerate(self.metadata):
            if m.get("doc_id") in doc_ids:
                removed += 1
            else:
                kept_embeddings.append(self.embeddings[i])
                kept_metadata.append(m)
        self.embeddings = kept_embeddings
        self.metadata = kept_metadata
        self._save()
        return removed
    
    def add_document(self, doc_id: str, text: str, metadata: Dict[str, Any]):
        """Add a document to the vector store"""
        model = self._get_model()
        if model is None:
            return
        
        # Truncate text if too long
        text = text[:8000]
        
        # Generate embedding
        embedding = model.encode(text, convert_to_numpy=True)
        
        # Check if document already exists
        for i, m in enumerate(self.metadata):
            if m.get("doc_id") == doc_id:
                self.embeddings[i] = embedding
                self.metadata[i] = {**metadata, "doc_id": doc_id, "text": text[:500]}
                return
        
        self.embeddings.append(embedding)
        self.metadata.append({**metadata, "doc_id": doc_id, "text": text[:500]})
    
    def add_batch(self, documents: List[Dict[str, Any]]):
        """Add multiple documents efficiently"""
        model = self._get_model()
        if model is None or not documents:
            return
        
        texts = [d["text"][:8000] for d in documents]
        embeddings = model.encode(texts, convert_to_numpy=True, show_progress_bar=True)
        
        for i, doc in enumerate(documents):
            self.embeddings.append(embeddings[i])
            self.metadata.append({
                "doc_id": doc["id"],
                "filename": doc.get("filename", "Unknown"),
                "category": doc.get("category", "Unknown"),
                "text": doc["text"][:500]
            })
        
        self._save()
    
    def search(self, query: str, n_results: int = 10,
               category: Optional[str] = None) -> List[Dict[str, Any]]:
        """Semantic search for documents"""
        model = self._get_model()
        if model is None or not self._ensure_matrix():
            return []

        query_embedding = model.encode(query, convert_to_numpy=True)

        query_norm = np.linalg.norm(query_embedding)
        similarities = np.dot(self._embeddings_matrix, query_embedding) / (self._embeddings_norms * query_norm)
        
        # Get top results
        results = []
        indices = np.argsort(similarities)[::-1]
        
        for idx in indices:
            meta = self.metadata[idx]
            
            # Filter by category if specified
            if category and meta.get("category") != category:
                continue
            
            results.append({
                "id": meta.get("doc_id"),
                "filename": meta.get("filename", "Unknown"),
                "path": meta.get("path", ""),
                "category": meta.get("category", "Unknown"),
                "subcategory": meta.get("subcategory", ""),
                "text": meta.get("text", ""),
                "score": float(similarities[idx])
            })
            
            if len(results) >= n_results:
                break
        
        return results
    
    def get_count(self) -> int:
        """Get number of documents in store"""
        if self._embeddings_matrix is not None:
            return self._embeddings_matrix.shape[0]
        return getattr(self, '_embedding_count', len(self.embeddings))


def build_index(base_path: str, force: bool = False, 
                index_progress_callback=None, embedding_progress_callback=None):
    """Build database and vector store from extracted documents
    
    Args:
        base_path: Path to the project root
        force: If True, re-index all documents. If False, only index new documents.
        index_progress_callback: Optional callback(current, total) for indexing progress
        embedding_progress_callback: Optional callback(current, total) for embedding progress
    """
    from tqdm import tqdm
    
    base_path = Path(base_path)
    extracted_dir = base_path / "extracted_text"
    
    if not extracted_dir.exists():
        print("No extracted text found. Run extractor.py first.")
        return
    
    # Initialize stores
    db = Database(str(base_path / "epstein.db"))
    vector_store = VectorStore(str(base_path / "vector_store"))
    
    # Load PDF index
    index_file = extracted_dir / "index.json"
    all_files = {}
    
    if index_file.exists():
        with open(index_file) as f:
            pdf_index = json.load(f)
            all_files.update(pdf_index.get("files", {}))
            print(f"Found {len(pdf_index.get('files', {}))} PDF documents")
    
    # Load image index (OCR results)
    image_index_file = extracted_dir / "image_index.json"
    if image_index_file.exists():
        with open(image_index_file) as f:
            image_index = json.load(f)
            all_files.update(image_index.get("files", {}))
            print(f"Found {len(image_index.get('files', {}))} image files")
    
    # Load media index (audio/video transcriptions)
    media_index_file = extracted_dir / "media_index.json"
    if media_index_file.exists():
        with open(media_index_file) as f:
            media_index = json.load(f)
            all_files.update(media_index.get("files", {}))
            print(f"Found {len(media_index.get('files', {}))} audio/video files")
    
    # If no index files (e.g. server-only deploy): discover from extracted_text/*.json
    index_file_names = {"index.json", "image_index.json", "media_index.json", "failed_pdf_files.json", "failed_media_files.json"}
    if not all_files:
        print("No index files found; discovering from extracted_text/*.json...")
        for jf in extracted_dir.iterdir():
            if jf.suffix != ".json" or jf.name in index_file_names or jf.name.startswith("index."):
                continue
            try:
                with open(jf) as f:
                    data = json.load(f)
                doc_id = data.get("id") or jf.stem
                all_files[doc_id] = {
                    "filename": data.get("filename", jf.name),
                    "path": data.get("path", ""),
                    "category": data.get("category", "Unknown"),
                    "subcategory": data.get("subcategory", ""),
                    "page_count": data.get("page_count", 0),
                    "char_count": data.get("char_count", 0),
                }
            except Exception:
                continue
        if all_files:
            print(f"Discovered {len(all_files)} documents from JSON files.")
    if not all_files:
        print("No documents to index.")
        return
    
    # Deduplicate by path: same logical file can appear under old (absolute-path) and new (relative-path) hash
    def _canonical_doc_hash(path: str, base: Path) -> Optional[str]:
        try:
            full = base / path
            if full.exists():
                size = full.stat().st_size
                return hashlib.md5(f"{path}_{size}".encode()).hexdigest()
        except Exception:
            pass
        return None
    
    path_to_entry = {}  # path -> (file_id, file_info)
    no_path_entries = {}  # file_id -> file_info for entries without path
    for file_id, file_info in all_files.items():
        path = file_info.get("path")
        if not path:
            no_path_entries[file_id] = file_info
            continue
        canonical = _canonical_doc_hash(path, base_path)
        if path not in path_to_entry:
            path_to_entry[path] = (file_id, file_info)
        else:
            existing_id, existing_info = path_to_entry[path]
            # Prefer file_id that equals canonical hash; otherwise keep first seen
            if canonical and file_id == canonical:
                path_to_entry[path] = (file_id, file_info)
            elif canonical and existing_id == canonical:
                pass  # keep existing
            # else keep first (existing)
    all_files_deduped = {fid: info for fid, info in path_to_entry.values()}
    all_files_deduped.update(no_path_entries)
    deduped_count = len(all_files) - len(all_files_deduped)
    if deduped_count > 0:
        print(f"  Deduplicated by path: {deduped_count} duplicate path(s) removed → {len(all_files_deduped)} unique documents")
    
    # Get already indexed document IDs and paths (skip unless force)
    existing_vector_ids = set()
    existing_db_ids = set()
    existing_paths = set()
    if not force:
        existing_vector_ids = vector_store.get_indexed_doc_ids()
        existing_db_ids = db.get_indexed_doc_ids()
        existing_paths = db.get_indexed_paths()
        print(f"  {len(existing_vector_ids)} documents already in vector store")
        print(f"  {len(existing_db_ids)} documents already in database")
    
    # Filter to only new documents (by id and by path so we don't re-insert same path under different hash)
    to_index = {
        k: v for k, v in all_files_deduped.items()
        if (force or k not in existing_vector_ids or k not in existing_db_ids)
        and (force or v.get("path") not in existing_paths)
    }
    
    if not to_index:
        print("✓ Index is up to date! No new documents to process.")
        print(f"  SQLite documents: {db.get_stats()['total_documents']}")
        print(f"  Vector embeddings: {vector_store.get_count()}")
        return
    
    print(f"Indexing {len(to_index)} new documents (skipping {len(all_files_deduped) - len(to_index)} already indexed)...")
    
    # Batches for efficient processing
    vector_batch = []
    db_batch = []
    batch_size = 256  # Increased from 100 for better GPU utilization
    total_docs = len(to_index)
    processed_count = 0
    embedding_batches_total = (total_docs // batch_size) + (1 if total_docs % batch_size else 0)
    embedding_batches_done = 0
    
    for file_id, file_info in tqdm(to_index.items(), desc="Indexing"):
        # Load full document data
        doc_file = extracted_dir / f"{file_id}.json"
        if not doc_file.exists():
            continue
        
        with open(doc_file) as f:
            doc = json.load(f)
        
        # Ensure doc has id (older extracted JSON may lack it)
        if "id" not in doc:
            doc["id"] = file_id
        
        # Ensure doc has path and filename (legacy or discovery-built JSON may lack them)
        if "path" not in doc or doc.get("path") is None:
            doc["path"] = file_info.get("path", "")
        if "filename" not in doc or not doc["filename"]:
            doc["filename"] = Path(doc.get("path", "")).name or "unknown"
        
        # Add to SQLite batch
        db_batch.append(doc)
        
        # Prepare for vector store
        full_text = doc.get("full_text", "")
        if full_text and len(full_text) > 100:
            vector_batch.append({
                "id": file_id,
                "text": full_text,
                "filename": doc["filename"],
                "category": doc.get("category", "Unknown")
            })
        
        processed_count += 1
        
        # Call index progress callback
        if index_progress_callback:
            try:
                index_progress_callback(processed_count, total_docs)
            except:
                pass
        
        # Process batches
        if len(db_batch) >= batch_size:
            db.insert_documents_batch(db_batch)
            db_batch = []
        
        if len(vector_batch) >= batch_size:
            vector_store.add_batch(vector_batch)
            embedding_batches_done += 1
            vector_batch = []
            
            # Call embedding progress callback
            if embedding_progress_callback:
                try:
                    embedding_progress_callback(embedding_batches_done * batch_size, total_docs)
                except:
                    pass
    
    # Process remaining batches
    if db_batch:
        db.insert_documents_batch(db_batch)
    if vector_batch:
        vector_store.add_batch(vector_batch)
        embedding_batches_done += 1
        
        # Final embedding progress callback
        if embedding_progress_callback:
            try:
                embedding_progress_callback(total_docs, total_docs)
            except:
                pass
    
    print(f"\nIndexing complete!")
    print(f"  SQLite documents: {db.get_stats()['total_documents']}")
    print(f"  Vector embeddings: {vector_store.get_count()}")


if __name__ == "__main__":
    import sys
    default_base = Path(__file__).resolve().parent.parent
    base_path = sys.argv[1] if len(sys.argv) > 1 else str(default_base)
    build_index(base_path)
