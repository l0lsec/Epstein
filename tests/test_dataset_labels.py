"""DOJ data-set labels and nginx cache opt-out for query-dependent API responses.
Run: python -m unittest discover -s tests"""

import os
import sys
import tempfile
import types
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "backend"))

try:
    import extractor  # noqa: F401
except ImportError:  # the PDF stack isn't needed here
    _stub = types.ModuleType("extractor")
    _stub.extract_email_date = lambda *a, **k: None
    sys.modules["extractor"] = _stub

from database import Database  # noqa: E402

try:
    from fastapi.testclient import TestClient
except ImportError:  # the server's own dependencies aren't installed (e.g. plain system Python)
    TestClient = None


def _doc(doc_id, filename, subcategory, category="DOJ Disclosures"):
    return {"id": doc_id, "filename": filename, "path": f"{category}/{filename}",
            "category": category, "subcategory": subcategory, "full_text": "x"}


class RelabelEftaSubcategories(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Database(os.path.join(self.tmp.name, "epstein.db"))
        self.db.insert_documents_batch([
            _doc("e1", "EFTA00000001.pdf", "Evidence Files"),           # -> Data Set 1
            _doc("e2", "EFTA00003500.pdf", "Evidence Files"),           # -> Data Set 2
            _doc("e4", "EFTA00007000.pdf", "Evidence Files"),           # -> Data Set 4
            _doc("e4old", "EFTA00007000_20250101_000000.pdf", "Evidence Files"),  # archived too
            _doc("e8", "EFTA00020000.pdf", ""),                         # -> Data Set 8
            _doc("keep-folder", "EFTA00000002.pdf", "Data Set 9"),      # folder label wins
            _doc("keep-range", "EFTA09999999.pdf", "Evidence Files"),   # no known range
            _doc("keep-named", "C. Contact Book (Redacted).pdf", "Contact Books"),
            _doc("keep-other-cat", "EFTA00000003.pdf", "Evidence Files", category="FOIA"),
        ])

    def tearDown(self):
        self.tmp.cleanup()

    def sub(self, doc_id):
        return self.db.get_document(doc_id, include_full_text=False, include_hidden=True)["subcategory"]

    def test_relabel(self):
        changed = self.db.relabel_efta_subcategories()
        self.assertEqual(changed, {"Data Set 1": 1, "Data Set 2": 1, "Data Set 4": 2, "Data Set 8": 1})
        for doc_id, want in [("e1", "Data Set 1"), ("e2", "Data Set 2"), ("e4", "Data Set 4"),
                             ("e4old", "Data Set 4"), ("e8", "Data Set 8"),
                             ("keep-folder", "Data Set 9"), ("keep-range", "Evidence Files"),
                             ("keep-named", "Contact Books"), ("keep-other-cat", "Evidence Files")]:
            self.assertEqual(self.sub(doc_id), want, doc_id)
        self.assertEqual(self.db.relabel_efta_subcategories(), {})  # idempotent


class MergeDojMediaSubcategories(unittest.TestCase):
    def test_merge(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Database(os.path.join(tmp, "epstein.db"))
            db.insert_documents_batch([
                _doc("a1", "Day 1 - Part 1 - 7_24_25_Tallahassee.003.wav", "Audio Recording"),
                _doc("a2", "Day 2 - Test - xxx7_25.001.wav", "Audio Recording"),
                _doc("a-other", "press call.wav", "Audio Recording"),        # no rule: kept
                _doc("v1", "2019.08.09 - 6 p.m. (18.00.09 - 19.00.33).mp4", "Video Recording"),
                _doc("v2", "video1.mp4", "Video Recording"),
                _doc("bop", "2019.08.10 - 3 a.m. (03.00.11 - 04.00.36).mp4", "BOP Video Footage"),
                _doc("foia-v", "clip.mp4", "Video Recording", category="FOIA"),  # other category
            ])
            self.assertEqual(db.merge_doj_media_subcategories(),
                             {"Maxwell Proffer": 2, "BOP Video Footage": 2})
            sub = lambda i: db.get_document(i, include_full_text=False)["subcategory"]
            for doc_id, want in [("a1", "Maxwell Proffer"), ("a2", "Maxwell Proffer"),
                                 ("a-other", "Audio Recording"), ("v1", "BOP Video Footage"),
                                 ("v2", "BOP Video Footage"), ("bop", "BOP Video Footage"),
                                 ("foia-v", "Video Recording")]:
                self.assertEqual(sub(doc_id), want, doc_id)
            self.assertEqual(db.merge_doj_media_subcategories(), {})  # idempotent


@unittest.skipIf(TestClient is None, "FastAPI/httpx not installed")
class NginxCacheOptOut(unittest.TestCase):
    """Query-dependent /api GETs carry X-Accel-Expires: 0 so nginx (whose cache key drops the
    query string) never serves one variant for another. The header is set even on errors."""

    @classmethod
    def setUpClass(cls):
        if "server" not in sys.modules:
            cls.tmp = tempfile.TemporaryDirectory()
            os.environ["EPSTEIN_BASE_PATH"] = cls.tmp.name
            os.environ.setdefault("ADMIN_API_KEY", "test-admin-key")
        import server
        cls.client = TestClient(server.app)

    def accel(self, path):
        return self.client.get(path).headers.get("x-accel-expires")

    def test_query_dependent_routes_opt_out(self):
        for path in ("/api/subcategories?category=FOIA", "/api/subcategories",
                     "/api/categories", "/api/categories?keyword=maxwell",
                     "/api/documents?category=FOIA&limit=1", "/api/version-diff?old=a&new=b"):
            self.assertEqual(self.accel(path), "0", path)

    def test_query_free_routes_stay_cacheable(self):
        for path in ("/api/stats", "/api/bootstrap"):
            self.assertIsNone(self.accel(path), path)


if __name__ == "__main__":
    unittest.main()
