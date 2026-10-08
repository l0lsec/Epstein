"""Admin toggle for the public "Altered by DOJ" features (setting altered_documents_enabled).
Run: python -m unittest discover -s tests  (needs the server's FastAPI/httpx deps; skipped otherwise)."""

import os
import sys
import tempfile
import types
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "backend"))

try:
    from fastapi.testclient import TestClient
except ImportError:  # the server's own dependencies aren't installed (e.g. plain system Python)
    TestClient = None
try:
    import fitz  # PyMuPDF: real fixture PDFs so in-PDF find/highlight run for real
except ImportError:
    fitz = None

ADMIN_KEY = "test-admin-key"
SETTING = "altered_documents_enabled"

# efta 1: exposed (public before/after); efta 2: cleared (legitimate redaction); efta 3: pending review.
CANON, OLD = "canon-exposed", "old-exposed"
CANON_CLEARED, OLD_CLEARED = "canon-cleared", "old-cleared"
CANON_PENDING = "canon-pending"
PLAIN = "plain-doc"
OLD_TEXT = "Meeting with Jeffrey Smith at the office.\nTransfer to Zurich."
NEW_TEXT = "Meeting with [REDACTED] at the office.\nTransfer to Zurich."


@unittest.skipIf(TestClient is None, "FastAPI/httpx not installed")
class AlteredDocsToggle(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        base = cls.tmp.name
        # server.py reads these at import time (paths, admin key, log dir).
        os.environ["EPSTEIN_BASE_PATH"] = base
        os.environ["ADMIN_API_KEY"] = ADMIN_KEY
        # The PDF/LLM stacks aren't needed for these routes; stub them when absent so this runs anywhere.
        try:
            import extractor  # noqa: F401
        except ImportError:
            stub = types.ModuleType("extractor")
            stub.extract_email_date = lambda *a, **k: None
            sys.modules["extractor"] = stub
        import server
        from database import Database
        cls.server = server
        if str(server.BASE_PATH) != base:
            raise unittest.SkipTest("server was imported earlier with another EPSTEIN_BASE_PATH")

        db = Database(os.path.join(base, "epstein.db"))
        with db.get_connection() as conn:
            def doc(doc_id, filename, text):
                conn.execute(
                    "INSERT INTO documents (id, filename, path, category, subcategory, file_type, "
                    "page_count, char_count, full_text) VALUES (?,?,?,?,?,?,?,?,?)",
                    (doc_id, filename, filename, "DOJ", "ds1", "pdf", 1, len(text), text),
                )
                if fitz:
                    pdf = fitz.open()
                    pdf.new_page().insert_text((72, 72), text)
                    pdf.save(os.path.join(base, filename))
                    pdf.close()
                else:
                    with open(os.path.join(base, filename), "wb") as f:
                        f.write(b"%PDF-1.4 " + doc_id.encode())

            def alt(efta, canon, old, old_filename, status, removed):
                conn.execute(
                    "INSERT INTO document_alterations (efta_num, file_type, dataset_num, canonical_id, "
                    "canonical_filename, old_id, old_filename, lines_removed, altered_on, review_status) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (efta, "pdf", 1, canon, f"EFTA{efta:08d}.pdf", old, old_filename, removed,
                     "20260101_120000", status),
                )

            doc(CANON, "EFTA00000001.pdf", NEW_TEXT)
            doc(OLD, "EFTA00000001_20250101_000000.pdf", OLD_TEXT)
            doc(CANON_CLEARED, "EFTA00000002.pdf", NEW_TEXT)
            doc(OLD_CLEARED, "EFTA00000002_20250101_000000.pdf", OLD_TEXT)
            doc(CANON_PENDING, "EFTA00000003.pdf", NEW_TEXT)
            doc(PLAIN, "report.pdf", "An ordinary document.")
            alt(1, CANON, OLD, "EFTA00000001_20250101_000000.pdf", "exposed", 1)
            alt(2, CANON_CLEARED, OLD_CLEARED, "EFTA00000002_20250101_000000.pdf", "cleared", 1)
            alt(3, CANON_PENDING, None, None, "pending", 4)
            conn.commit()

        # Pre-generated thumbnails, as ingest leaves them: the route must gate before serving them.
        os.makedirs(server.THUMBNAILS_PATH, exist_ok=True)
        for doc_id in (OLD, PLAIN):
            with open(server.THUMBNAILS_PATH / f"{doc_id}.jpg", "wb") as f:
                f.write(b"\xff\xd8\xff\xe0 thumb")

        server.db = db
        cls.db = db
        cls.client = TestClient(server.app)

    @classmethod
    def tearDownClass(cls):
        cls.server.db = None
        cls.tmp.cleanup()

    def setUp(self):
        # Every test starts from a fresh install: setting never written, caches cold.
        with self.db.get_connection() as conn:
            conn.execute("DELETE FROM settings WHERE key = ?", (SETTING,))
            conn.commit()
        self.server._settings_cache.invalidate()
        self.server._bootstrap_cache.invalidate()

    # -- helpers -------------------------------------------------------------------------------

    def get(self, path):
        return self.client.get(path)

    def set_toggle(self, enabled, key=ADMIN_KEY):
        headers = {"x-api-key": key} if key is not None else {}
        return self.client.post("/api/admin/settings", headers=headers,
                                json={"key": SETTING, "value": "true" if enabled else "false"})

    def archived_routes(self, doc_id):
        return [f"/api/documents/{doc_id}", f"/api/documents/{doc_id}/text",
                f"/api/documents/{doc_id}/file", f"/api/documents/{doc_id}/thumbnail"]

    def assert_hidden(self):
        """Every public alteration surface is withheld."""
        r = self.get("/api/altered-documents?limit=200")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json(), {"altered_documents": [], "total": 0})
        for canon in (CANON, CANON_PENDING):
            self.assertEqual(self.get(f"/api/documents/{canon}/alteration").json(), {"altered": False})
        self.assertEqual(self.get(f"/api/version-diff?old={OLD}&new={CANON}").status_code, 404)
        for route in self.archived_routes(OLD):
            self.assertEqual(self.get(route).status_code, 404, route)
        self.assertIs(self.get("/api/settings").json()[SETTING], False)
        self.assertIs(self.get("/api/bootstrap").json()["settings"][SETTING], False)

    def assert_shown(self):
        """The exposed alteration is published on every public surface."""
        data = self.get("/api/altered-documents?limit=200").json()
        self.assertEqual(data["total"], 1)
        self.assertEqual([(d["new_id"], d["old_id"]) for d in data["altered_documents"]], [(CANON, OLD)])
        badge = self.get(f"/api/documents/{CANON}/alteration").json()
        self.assertTrue(badge["altered"] and badge["exposed"])
        self.assertEqual((badge["old_id"], badge["new_id"]), (OLD, CANON))
        pending = self.get(f"/api/documents/{CANON_PENDING}/alteration").json()
        self.assertTrue(pending["altered"])
        self.assertFalse(pending["exposed"])
        self.assertNotIn("old_id", pending)
        diff = self.get(f"/api/version-diff?old={OLD}&new={CANON}")
        self.assertEqual(diff.status_code, 200)
        self.assertGreaterEqual(diff.json()["removed"], 1)
        for route in self.archived_routes(OLD):
            self.assertEqual(self.get(route).status_code, 200, route)
        self.assertIs(self.get("/api/settings").json()[SETTING], True)
        self.assertIs(self.get("/api/bootstrap").json()["settings"][SETTING], True)

    # -- tests ---------------------------------------------------------------------------------

    def test_default_hides_everything(self):
        # The data really is there (so the absences below aren't vacuous)...
        self.assertEqual(self.db.count_exposed_alterations(), 1)
        self.assertTrue(self.db.is_public_servable(OLD))
        # ...but with the setting never written, nothing about it is public.
        self.assertIsNone(self.db.get_setting(SETTING))
        self.assert_hidden()
        self.assertEqual(self.get(f"/api/documents/{OLD}/summary").status_code, 404)

    def test_enabled_shows_then_disable_hides_again(self):
        # Warm both caches while OFF: the toggle must take effect without waiting for them to expire.
        self.assert_hidden()
        r = self.set_toggle(True)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(self.db.get_setting(SETTING), "true")
        self.assert_shown()
        self.assertEqual(self.set_toggle(False).status_code, 200)
        self.assertEqual(self.db.get_setting(SETTING), "false")
        self.assert_hidden()

    def test_regressions(self):
        for enabled in (False, True):
            self.assertEqual(self.set_toggle(enabled).status_code, 200)
            # Ordinary and canonical documents are untouched by the toggle.
            for doc_id in (PLAIN, CANON):
                for route in self.archived_routes(doc_id)[:3]:
                    self.assertEqual(self.get(route).status_code, 200, (enabled, route))
            self.assertEqual(self.get(f"/api/documents/{PLAIN}/thumbnail").status_code, 200)
            # A cleared (legitimate) redaction never becomes public, toggle or not.
            for route in self.archived_routes(OLD_CLEARED):
                self.assertEqual(self.get(route).status_code, 404, (enabled, route))
            self.assertEqual(self.get(f"/api/documents/{CANON_CLEARED}/alteration").json(), {"altered": False})
            self.assertEqual(self.get(f"/api/version-diff?old={OLD_CLEARED}&new={CANON_CLEARED}").status_code, 404)
        # Flipping it still requires the admin key.
        self.assertEqual(self.set_toggle(False).status_code, 200)
        self.assertEqual(self.set_toggle(True, key=None).status_code, 401)
        self.assertEqual(self.set_toggle(True, key="wrong").status_code, 401)
        self.assertEqual(self.db.get_setting(SETTING), "false")
        self.assertEqual(self.get("/api/altered-documents").json()["total"], 0)

    def test_archived_versions_are_never_edge_cached(self):
        # A cached copy would outlive the admin hiding it again, so nothing archived may be stored.
        self.assertEqual(self.set_toggle(True).status_code, 200)
        self.db.save_summary(OLD, "Cached summary of the pre-change version.")
        routes = self.archived_routes(OLD) + [
            f"/api/version-diff?old={OLD}&new={CANON}",
            f"/api/documents/{OLD}/summary",
        ]
        if fitz:
            routes += [f"/api/documents/{OLD}/pdf-find?t=Smith", f"/api/documents/{OLD}/highlighted?t=Smith"]
        for route in routes:
            r = self.get(route)
            self.assertEqual(r.status_code, 200, route)
            self.assertEqual(r.headers.get("cache-control"), "private, no-store", route)
        if fitz:
            # Find/highlight really parsed the PDF (the highlighted copy, not the plain-file fallback).
            pages = self.get(f"/api/documents/{OLD}/pdf-find?t=Smith").json()["pages"]
            self.assertEqual([p["page"] for p in pages], [1])
            self.assertNotEqual(self.get(f"/api/documents/{OLD}/highlighted?t=Smith").content,
                                self.get(f"/api/documents/{OLD}/file").content)

    def test_normal_documents_keep_edge_caching(self):
        for enabled in (False, True):
            self.assertEqual(self.set_toggle(enabled).status_code, 200)
            for doc_id in (PLAIN, CANON):
                r = self.get(f"/api/documents/{doc_id}/file")
                self.assertEqual(r.headers.get("cache-control"), "public, max-age=604800", (enabled, doc_id))
                for route in (f"/api/documents/{doc_id}", f"/api/documents/{doc_id}/text"):
                    r = self.get(route)
                    self.assertEqual(r.status_code, 200, route)
                    self.assertNotIn("no-store", r.headers.get("cache-control", ""), route)
            r = self.get(f"/api/documents/{PLAIN}/thumbnail")
            self.assertEqual(r.headers.get("cache-control"), "public, max-age=2592000, immutable", enabled)


if __name__ == "__main__":
    unittest.main()
