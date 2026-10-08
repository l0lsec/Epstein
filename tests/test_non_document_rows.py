"""Non-document rows (app files, extractor logs) must not reach the public category list.
Run: python -m unittest discover -s tests"""

import json
import os
import sys
import tempfile
import types
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "backend"))

# The PDF stack isn't needed here; stub it (and tqdm, used by build_index) when absent.
try:
    import extractor  # noqa: F401
except ImportError:
    _stub = types.ModuleType("extractor")
    _stub.extract_email_date = lambda *a, **k: None
    sys.modules["extractor"] = _stub
try:
    import tqdm  # noqa: F401
except ImportError:
    _tqdm = types.ModuleType("tqdm")
    _tqdm.tqdm = lambda it, **_k: it
    sys.modules["tqdm"] = _tqdm

from database import Database, build_index, is_non_document_entry  # noqa: E402


def _doc(doc_id, path, category, filename=None):
    return {
        "id": doc_id, "filename": filename or os.path.basename(path) or "unknown", "path": path,
        "category": category, "subcategory": "", "full_text": "text", "char_count": 4,
    }


REAL = [
    _doc("real-doj", "DOJ Disclosures/DataSet 1/EFTA00000001.pdf", "DOJ Disclosures"),
    _doc("real-court", "CourtRecords/Giuffre v Maxwell/filing.pdf", "Court Records"),
    _doc("real-root", "loose-file.pdf", "Unknown"),  # top-level file: no folder, so kept
    # A real EFTA file that once landed in an app dir keeps its curated category, so it stays.
    _doc("real-efta-in-tmp", "tmp/EFTA00009999.pdf", "DOJ Disclosures"),
]
JUNK = [
    _doc("b3a8f0a414ecce2c4273ed86cd85dd3c", "frontend/og-image.png", "frontend"),
    _doc("failed_media_files", "", "Unknown", filename="unknown"),
    _doc("win-junk", "backend\\notes.png", "backend"),
]


class IsNonDocumentEntry(unittest.TestCase):
    def test_classification(self):
        for d in JUNK:
            self.assertTrue(is_non_document_entry(d["id"], d["path"], d["category"]), d)
        for d in REAL:
            self.assertFalse(is_non_document_entry(d["id"], d["path"], d["category"]), d)
        # A real category folder whose name merely contains an app-dir word is kept.
        self.assertFalse(is_non_document_entry("x", "FOIA/frontend-requests/a.pdf"))


class PurgeNonDocuments(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Database(os.path.join(self.tmp.name, "epstein.db"))
        self.db.insert_documents_batch(REAL + JUNK)

    def tearDown(self):
        self.tmp.cleanup()

    def _categories(self):
        return {c["category"] for c in self.db.get_category_counts(include_hidden=False)}

    def test_purge_removes_only_junk(self):
        # Positive control: the junk is visible before the purge.
        self.assertIn("frontend", self._categories())
        removed = self.db.purge_non_documents()
        self.assertEqual({r["id"] for r in removed}, {d["id"] for d in JUNK})
        self.assertEqual(self.db.get_indexed_doc_ids(), {d["id"] for d in REAL})
        self.assertEqual(self._categories(), {"DOJ Disclosures", "Court Records", "Unknown"})
        # FTS stays in sync (documents_ad trigger): the purged png can't be found.
        hits = self.db.search_fulltext("text", limit=50)
        self.assertEqual({h["id"] for h in hits}, {d["id"] for d in REAL})
        self.assertEqual(self.db.purge_non_documents(), [])  # idempotent


class BuildIndexSkipsJunk(unittest.TestCase):
    def test_build_index_skips_and_purges(self):
        import database
        with tempfile.TemporaryDirectory() as base:
            ext = os.path.join(base, "extracted_text")
            os.makedirs(ext)
            files = {}
            for d in REAL + JUNK:
                with open(os.path.join(ext, f"{d['id']}.json"), "w") as f:
                    json.dump(d, f)
                files[d["id"]] = {"filename": d["filename"], "path": d["path"], "category": d["category"]}
            with open(os.path.join(ext, "image_index.json"), "w") as f:
                json.dump({"files": files}, f)
            # A stale junk row already in the DB from an older run.
            Database(os.path.join(base, "epstein.db")).insert_documents_batch([JUNK[0]])

            class FakeVectors:
                def __init__(self, *_a): pass
                def get_indexed_doc_ids(self): return set()
                def add_batch(self, _b): pass
                def get_count(self): return 0

            orig = database.VectorStore
            database.VectorStore = FakeVectors
            try:
                build_index(base)
            finally:
                database.VectorStore = orig
            ids = Database(os.path.join(base, "epstein.db")).get_indexed_doc_ids()
            self.assertEqual(ids, {d["id"] for d in REAL})


if __name__ == "__main__":
    unittest.main()
