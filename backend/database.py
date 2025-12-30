"""
Database Module
Handles SQLite for metadata, full-text search, and vector search
"""

import os
import json
import sqlite3
import pickle
from pathlib import Path
from typing import List, Dict, Any, Optional
from contextlib import contextmanager
import numpy as np


class Database:
    """SQLite database for document metadata and search"""
    
    def __init__(self, db_path: str):
        self.db_path = db_path
        self._init_db()
    
    @contextmanager
    def get_connection(self):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
        finally:
            conn.close()
    
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
                
                CREATE TRIGGER IF NOT EXISTS documents_au AFTER UPDATE ON documents BEGIN
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
    
    def rebuild_fts(self):
        """Rebuild the FTS5 index to fix sync issues"""
        with self.get_connection() as conn:
            # Drop and recreate the FTS table and triggers
            conn.executescript("""
                DROP TRIGGER IF EXISTS documents_ai;
                DROP TRIGGER IF EXISTS documents_ad;
                DROP TRIGGER IF EXISTS documents_au;
                DROP TABLE IF EXISTS documents_fts;
                
                CREATE VIRTUAL TABLE documents_fts USING fts5(
                    id,
                    filename,
                    full_text,
                    content=documents,
                    content_rowid=rowid
                );
                
                -- Repopulate FTS from documents table
                INSERT INTO documents_fts(rowid, id, filename, full_text)
                SELECT rowid, id, filename, full_text FROM documents;
                
                -- Recreate triggers
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
        print("FTS index rebuilt successfully")
    
    def insert_document(self, doc: Dict[str, Any]):
        """Insert or update a document"""
        with self.get_connection() as conn:
            conn.execute("""
                INSERT OR REPLACE INTO documents 
                (id, filename, original_filename, path, category, subcategory, 
                 file_type, page_count, char_count, duration_seconds, full_text)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                doc.get("full_text", "")
            ))
            conn.commit()
    
    def search_fulltext(self, query: str, limit: int = 50, offset: int = 0, 
                        category: Optional[str] = None,
                        subcategory: Optional[str] = None,
                        file_type: Optional[str] = None) -> List[Dict[str, Any]]:
        """Full-text search across documents"""
        with self.get_connection() as conn:
            # Build query with optional filters
            sql = """
                SELECT 
                    d.id, d.filename, d.path, d.category, d.subcategory, d.file_type,
                    d.page_count, d.char_count, d.duration_seconds,
                    snippet(documents_fts, 2, '<mark>', '</mark>', '...', 64) as snippet,
                    bm25(documents_fts) as score
                FROM documents_fts
                JOIN documents d ON documents_fts.id = d.id
                WHERE documents_fts MATCH ?
            """
            params = [query]
            
            if category:
                sql += " AND d.category = ?"
                params.append(category)
            
            if subcategory:
                sql += " AND d.subcategory = ?"
                params.append(subcategory)
            
            if file_type:
                sql += " AND d.file_type = ?"
                params.append(file_type)
            
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
                               file_type: Optional[str] = None) -> int:
        """Count total full-text search results (for pagination)"""
        with self.get_connection() as conn:
            sql = """
                SELECT COUNT(*)
                FROM documents_fts
                JOIN documents d ON documents_fts.id = d.id
                WHERE documents_fts MATCH ?
            """
            params = [query]
            
            if category:
                sql += " AND d.category = ?"
                params.append(category)
            
            if subcategory:
                sql += " AND d.subcategory = ?"
                params.append(subcategory)
            
            if file_type:
                sql += " AND d.file_type = ?"
                params.append(file_type)
            
            cursor = conn.execute(sql, params)
            return cursor.fetchone()[0]
    
    def get_search_facets(self, query: str, 
                          category: Optional[str] = None,
                          subcategory: Optional[str] = None,
                          file_type: Optional[str] = None) -> Dict[str, Any]:
        """Get faceted counts for search results (category, subcategory, file_type breakdowns)"""
        with self.get_connection() as conn:
            # Base match condition
            base_match = "documents_fts MATCH ?"
            base_params = [query]
            
            # Category counts (unfiltered by category to show all options)
            category_sql = f"""
                SELECT d.category, COUNT(*) as count
                FROM documents_fts
                JOIN documents d ON documents_fts.id = d.id
                WHERE {base_match}
            """
            category_params = list(base_params)
            if file_type:
                category_sql += " AND d.file_type = ?"
                category_params.append(file_type)
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
                    WHERE {base_match} AND d.category = ?
                """
                subcategory_params = list(base_params) + [category]
                if file_type:
                    subcategory_sql += " AND d.file_type = ?"
                    subcategory_params.append(file_type)
                subcategory_sql += " GROUP BY d.subcategory ORDER BY count DESC"
                
                cursor = conn.execute(subcategory_sql, subcategory_params)
                subcategories = [{"subcategory": row[0], "count": row[1]} for row in cursor.fetchall() if row[0]]
            
            # File type counts (unfiltered by file_type to show all options)
            file_type_sql = f"""
                SELECT d.file_type, COUNT(*) as count
                FROM documents_fts
                JOIN documents d ON documents_fts.id = d.id
                WHERE {base_match}
            """
            file_type_params = list(base_params)
            if category:
                file_type_sql += " AND d.category = ?"
                file_type_params.append(category)
            if subcategory:
                file_type_sql += " AND d.subcategory = ?"
                file_type_params.append(subcategory)
            file_type_sql += " GROUP BY d.file_type ORDER BY count DESC"
            
            cursor = conn.execute(file_type_sql, file_type_params)
            file_types = [{"file_type": row[0], "count": row[1]} for row in cursor.fetchall()]
            
            return {
                "categories": categories,
                "subcategories": subcategories,
                "file_types": file_types
            }
    
    def get_document(self, doc_id: str) -> Optional[Dict[str, Any]]:
        """Get a single document by ID"""
        with self.get_connection() as conn:
            cursor = conn.execute(
                "SELECT * FROM documents WHERE id = ?", (doc_id,)
            )
            row = cursor.fetchone()
            return dict(row) if row else None
    
    def get_stats(self) -> Dict[str, Any]:
        """Get database statistics"""
        with self.get_connection() as conn:
            stats = {}
            
            # Total documents
            cursor = conn.execute("SELECT COUNT(*) FROM documents")
            stats["total_documents"] = cursor.fetchone()[0]
            
            # Total pages
            cursor = conn.execute("SELECT SUM(page_count) FROM documents")
            stats["total_pages"] = cursor.fetchone()[0] or 0
            
            # By category
            cursor = conn.execute("""
                SELECT category, COUNT(*) as count 
                FROM documents 
                GROUP BY category 
                ORDER BY count DESC
            """)
            stats["by_category"] = [dict(row) for row in cursor]
            
            # By subcategory
            cursor = conn.execute("""
                SELECT category, subcategory, COUNT(*) as count 
                FROM documents 
                GROUP BY category, subcategory 
                ORDER BY category, count DESC
            """)
            stats["by_subcategory"] = [dict(row) for row in cursor]
            
            # By file type
            cursor = conn.execute("""
                SELECT file_type, COUNT(*) as count 
                FROM documents 
                GROUP BY file_type 
                ORDER BY count DESC
            """)
            stats["by_file_type"] = [dict(row) for row in cursor]
            
            return stats
    
    def get_category_counts(self, keyword: Optional[str] = None) -> List[Dict[str, Any]]:
        """Get category counts, optionally filtered by keyword search"""
        with self.get_connection() as conn:
            if keyword:
                # Use FTS to filter by keyword
                escaped_keyword = keyword.replace('"', '""')
                sql = """
                    SELECT d.category, COUNT(*) as count
                    FROM documents d
                    JOIN documents_fts fts ON d.id = fts.id
                    WHERE documents_fts MATCH ?
                    GROUP BY d.category
                    ORDER BY count DESC
                """
                cursor = conn.execute(sql, [f'"{escaped_keyword}"*'])
            else:
                sql = """
                    SELECT category, COUNT(*) as count 
                    FROM documents 
                    GROUP BY category 
                    ORDER BY count DESC
                """
                cursor = conn.execute(sql)
            
            return [dict(row) for row in cursor]
    
    def get_all_documents(self, limit: int = 100, offset: int = 0, 
                          category: Optional[str] = None,
                          subcategory: Optional[str] = None,
                          file_type: Optional[str] = None,
                          filename: Optional[str] = None,
                          keyword: Optional[str] = None) -> List[Dict[str, Any]]:
        """Get all documents with pagination and filtering"""
        with self.get_connection() as conn:
            params = []
            conditions = []
            
            # If keyword search, use FTS join
            if keyword:
                sql = """
                    SELECT DISTINCT d.id, d.filename, d.path, d.category, d.subcategory, d.file_type, d.page_count, d.char_count, d.duration_seconds
                    FROM documents d
                    JOIN documents_fts fts ON d.id = fts.id
                    WHERE documents_fts MATCH ?
                """
                # Escape special FTS characters and add wildcards for partial matching
                escaped_keyword = keyword.replace('"', '""')
                params.append(f'"{escaped_keyword}"*')
            else:
                sql = """
                    SELECT id, filename, path, category, subcategory, file_type, page_count, char_count, duration_seconds
                    FROM documents
                """
            
            if category:
                conditions.append("category = ?" if not keyword else "d.category = ?")
                params.append(category)
            
            if subcategory:
                conditions.append("subcategory = ?" if not keyword else "d.subcategory = ?")
                params.append(subcategory)
            
            if file_type:
                conditions.append("file_type = ?" if not keyword else "d.file_type = ?")
                params.append(file_type)
            
            if filename:
                # Case-insensitive partial match on filename
                conditions.append("LOWER(filename) LIKE LOWER(?)" if not keyword else "LOWER(d.filename) LIKE LOWER(?)")
                params.append(f"%{filename}%")
            
            if conditions:
                if keyword:
                    sql += " AND " + " AND ".join(conditions)
                else:
                    sql += " WHERE " + " AND ".join(conditions)
            
            sql += " ORDER BY filename LIMIT ? OFFSET ?" if not keyword else " ORDER BY d.filename LIMIT ? OFFSET ?"
            params.extend([limit, offset])
            
            cursor = conn.execute(sql, params)
            return [dict(row) for row in cursor]
    
    def count_documents(self, category: Optional[str] = None,
                        subcategory: Optional[str] = None,
                        file_type: Optional[str] = None,
                        filename: Optional[str] = None,
                        keyword: Optional[str] = None) -> int:
        """Count documents with optional filters"""
        with self.get_connection() as conn:
            params = []
            conditions = []
            
            # If keyword search, use FTS join
            if keyword:
                sql = """
                    SELECT COUNT(DISTINCT d.id)
                    FROM documents d
                    JOIN documents_fts fts ON d.id = fts.id
                    WHERE documents_fts MATCH ?
                """
                escaped_keyword = keyword.replace('"', '""')
                params.append(f'"{escaped_keyword}"*')
            else:
                sql = "SELECT COUNT(*) FROM documents"
            
            if category:
                conditions.append("category = ?" if not keyword else "d.category = ?")
                params.append(category)
            
            if subcategory:
                conditions.append("subcategory = ?" if not keyword else "d.subcategory = ?")
                params.append(subcategory)
            
            if file_type:
                conditions.append("file_type = ?" if not keyword else "d.file_type = ?")
                params.append(file_type)
            
            if filename:
                conditions.append("LOWER(filename) LIKE LOWER(?)" if not keyword else "LOWER(d.filename) LIKE LOWER(?)")
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
        with self.get_connection() as conn:
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
        with self.get_connection() as conn:
            conn.executemany("""
                INSERT OR REPLACE INTO documents 
                (id, filename, original_filename, path, category, subcategory, 
                 file_type, page_count, char_count, duration_seconds, full_text)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, [(
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
                doc.get("full_text", "")
            ) for doc in documents])
            conn.commit()
    
    def get_indexed_doc_ids(self) -> set:
        """Get set of all document IDs currently in the database"""
        with self.get_connection() as conn:
            cursor = conn.execute("SELECT id FROM documents")
            return {row[0] for row in cursor.fetchall()}
    
    # =========================================================================
    # Settings Methods
    # =========================================================================
    
    def get_setting(self, key: str, default: str = None) -> Optional[str]:
        """Get a setting value by key"""
        with self.get_connection() as conn:
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
        with self.get_connection() as conn:
            cursor = conn.execute("SELECT key, value FROM settings")
            return {row["key"]: row["value"] for row in cursor.fetchall()}
    
    # =========================================================================
    # Pinned Documents Methods
    # =========================================================================
    
    def get_pinned_documents(self) -> List[Dict[str, Any]]:
        """Get all pinned documents with their details"""
        with self.get_connection() as conn:
            cursor = conn.execute("""
                SELECT 
                    p.document_id, p.reason, p.display_order, p.pinned_at,
                    d.filename, d.path, d.category, d.subcategory, d.file_type,
                    d.page_count, d.char_count
                FROM pinned_documents p
                JOIN documents d ON p.document_id = d.id
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
        
        self._load()
    
    def _load(self):
        """Load existing embeddings from disk"""
        if self.embeddings_file.exists() and self.metadata_file.exists():
            try:
                with open(self.embeddings_file, 'rb') as f:
                    self.embeddings = pickle.load(f)
                with open(self.metadata_file, 'rb') as f:
                    self.metadata = pickle.load(f)
                print(f"Loaded {len(self.embeddings)} embeddings from disk")
            except Exception as e:
                print(f"Error loading embeddings: {e}")
                self.embeddings = []
                self.metadata = []
    
    def _save(self):
        """Save embeddings to disk"""
        with open(self.embeddings_file, 'wb') as f:
            pickle.dump(self.embeddings, f)
        with open(self.metadata_file, 'wb') as f:
            pickle.dump(self.metadata, f)
    
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
        if model is None or not self.embeddings:
            return []
        
        # Encode query
        query_embedding = model.encode(query, convert_to_numpy=True)
        
        # Calculate cosine similarities
        embeddings_matrix = np.array(self.embeddings)
        similarities = np.dot(embeddings_matrix, query_embedding) / (
            np.linalg.norm(embeddings_matrix, axis=1) * np.linalg.norm(query_embedding)
        )
        
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
        return len(self.embeddings)


def build_index(base_path: str, force: bool = False):
    """Build database and vector store from extracted documents
    
    Args:
        base_path: Path to the project root
        force: If True, re-index all documents. If False, only index new documents.
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
    
    if not all_files:
        print("No index files found.")
        return
    
    # Get already indexed document IDs (skip unless force)
    existing_vector_ids = set()
    existing_db_ids = set()
    if not force:
        existing_vector_ids = vector_store.get_indexed_doc_ids()
        existing_db_ids = db.get_indexed_doc_ids()
        print(f"  {len(existing_vector_ids)} documents already in vector store")
        print(f"  {len(existing_db_ids)} documents already in database")
    
    # Filter to only new documents
    to_index = {k: v for k, v in all_files.items() 
                if force or k not in existing_vector_ids or k not in existing_db_ids}
    
    if not to_index:
        print("✓ Index is up to date! No new documents to process.")
        print(f"  SQLite documents: {db.get_stats()['total_documents']}")
        print(f"  Vector embeddings: {vector_store.get_count()}")
        return
    
    print(f"Indexing {len(to_index)} new documents (skipping {len(all_files) - len(to_index)} already indexed)...")
    
    # Batches for efficient processing
    vector_batch = []
    db_batch = []
    batch_size = 256  # Increased from 100 for better GPU utilization
    
    for file_id, file_info in tqdm(to_index.items(), desc="Indexing"):
        # Load full document data
        doc_file = extracted_dir / f"{file_id}.json"
        if not doc_file.exists():
            continue
        
        with open(doc_file) as f:
            doc = json.load(f)
        
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
        
        # Process batches
        if len(db_batch) >= batch_size:
            db.insert_documents_batch(db_batch)
            db_batch = []
        
        if len(vector_batch) >= batch_size:
            vector_store.add_batch(vector_batch)
            vector_batch = []
    
    # Process remaining batches
    if db_batch:
        db.insert_documents_batch(db_batch)
    if vector_batch:
        vector_store.add_batch(vector_batch)
    
    print(f"\nIndexing complete!")
    print(f"  SQLite documents: {db.get_stats()['total_documents']}")
    print(f"  Vector embeddings: {vector_store.get_count()}")


if __name__ == "__main__":
    import sys
    base_path = sys.argv[1] if len(sys.argv) > 1 else "/Users/user/Documents/Epstein"
    build_index(base_path)
