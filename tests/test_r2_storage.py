"""Document files served from Cloudflare R2 (backend/storage.py) with the local disk as fallback.

R2 is replaced by an in-memory fake behind httpx.MockTransport, so nothing leaves the machine.
Run: python -m unittest discover -s tests  (needs the server's FastAPI/httpx deps; skipped otherwise)."""

import datetime as dt
import os
import sys
import tempfile
import time
import types
import unittest
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "backend"))

try:
    import httpx
    from fastapi.testclient import TestClient
except ImportError:
    TestClient = None
try:
    import fitz
except ImportError:
    fitz = None

ADMIN_KEY = "test-admin-key"
ACCOUNT = "acct123"
BUCKET = "docs-test"
R2_HOST = f"{ACCOUNT}.r2.cloudflarestorage.com"


class FakeR2:
    """Just enough of the S3 API for storage.py: HEAD, GET and PUT on /<bucket>/<key>."""

    def __init__(self):
        self.objects = {}
        self.down = False
        self.requests = []

    def handler(self, request):
        self.requests.append((request.method, request.url))
        if self.down:
            raise httpx.ConnectError("R2 unreachable")
        assert request.url.host == R2_HOST
        assert "X-Amz-Signature" in request.url.params
        bucket, _, key = unquote(request.url.path).lstrip("/").partition("/")
        assert bucket == BUCKET
        if request.method == "PUT":
            self.objects[key] = request.content
            return httpx.Response(200)
        if key not in self.objects:
            return httpx.Response(404)
        body = self.objects[key]
        if request.method == "HEAD":
            return httpx.Response(200, headers={"content-length": str(len(body))})
        return httpx.Response(200, content=body)


class Signer(unittest.TestCase):
    def test_matches_aws_published_presigned_url_example(self):
        sys.modules.setdefault("httpx", types.ModuleType("httpx"))
        import storage
        url = storage.presign_url(
            "GET", "examplebucket.s3.amazonaws.com", "/test.txt",
            access_key="AKIAIOSFODNN7EXAMPLE", secret_key="wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY",
            region="us-east-1", service="s3", when=dt.datetime(2013, 5, 24, tzinfo=dt.timezone.utc),
            expires=86400)
        self.assertTrue(url.endswith(
            "X-Amz-Signature=aeeed9bbccd4d02ee5c0109b86d86835f995330da4c265957d157751f604d404"))


@unittest.skipIf(TestClient is None, "FastAPI/httpx not installed")
class R2Serving(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        base = Path(cls.tmp.name).resolve()
        os.environ.setdefault("EPSTEIN_BASE_PATH", str(base))
        os.environ.setdefault("ADMIN_API_KEY", ADMIN_KEY)
        try:
            import extractor  # noqa: F401
        except ImportError:
            stub = types.ModuleType("extractor")
            stub.extract_email_date = lambda *a, **k: None
            sys.modules["extractor"] = stub
        import server
        import storage
        from database import Database
        if server.ADMIN_API_KEY != ADMIN_KEY:
            raise unittest.SkipTest("server was imported earlier with another admin key")
        cls.server, cls.storage = server, storage

        # Point the (possibly already imported) server at this test's own folder.
        cls.saved = (server.BASE_PATH, server.THUMBNAILS_PATH, server.db)
        server.BASE_PATH = base
        server.THUMBNAILS_PATH = base / "thumbnails"
        cls.base = base

        cls.db = Database(str(base / "epstein.db"))
        with cls.db.get_connection() as conn:
            for doc_id, path in [
                ("in-r2", "DOJ Disclosures/Data Set 1/EFTA00000100.pdf"),
                ("local-only", "DOJ Disclosures/Data Set 1/EFTA00000101.pdf"),
                ("archived", "DOJ Disclosures/Data Set 1/EFTA00000102_20250101_000000.pdf"),
                ("r2-only", "House Disclosures/memo with spaces.pdf"),
            ]:
                conn.execute(
                    "INSERT INTO documents (id, filename, path, category, subcategory, file_type, "
                    "page_count, char_count, full_text) VALUES (?,?,?,?,?,?,?,?,?)",
                    (doc_id, Path(path).name, path, "DOJ", "ds1", "pdf", 1, 10, "Zurich transfer"),
                )
            conn.commit()
        server.db = cls.db
        cls.client = TestClient(server.app)

    @classmethod
    def tearDownClass(cls):
        server = cls.server
        server.BASE_PATH, server.THUMBNAILS_PATH, server.db = cls.saved
        cls.tmp.cleanup()

    def pdf_bytes(self, text):
        if not fitz:
            return b"%PDF-1.4 " + text.encode()
        pdf = fitz.open()
        pdf.new_page().insert_text((72, 72), text)
        data = pdf.tobytes()
        pdf.close()
        return data

    def setUp(self):
        st = self.storage
        self.saved_cfg = (st._ACCOUNT_ID, st._ACCESS_KEY, st._SECRET_KEY, st._BUCKET, st._SERVE,
                          st._client, st._CACHE_MAX_BYTES, st._base_path, st._cache_dir)
        self.r2 = FakeR2()
        st._ACCOUNT_ID, st._ACCESS_KEY, st._SECRET_KEY, st._BUCKET = ACCOUNT, "AK", "SK", BUCKET
        st._SERVE = True
        st._client = httpx.Client(transport=httpx.MockTransport(self.r2.handler))
        st._size_cache.clear()
        st.configure(self.base)

        # Fresh files every test: in-r2 and archived exist in both places, local-only only on
        # disk, r2-only only in the bucket.
        for doc_id, rel, where in [
            ("in-r2", "DOJ Disclosures/Data Set 1/EFTA00000100.pdf", "both"),
            ("local-only", "DOJ Disclosures/Data Set 1/EFTA00000101.pdf", "local"),
            ("archived", "DOJ Disclosures/Data Set 1/EFTA00000102_20250101_000000.pdf", "both"),
            ("r2-only", "House Disclosures/memo with spaces.pdf", "r2"),
        ]:
            data = self.pdf_bytes(f"{doc_id} Zurich transfer")
            p = self.base / rel
            if where in ("both", "local"):
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_bytes(data)
            elif p.exists():
                p.unlink()
            if where in ("both", "r2"):
                self.r2.objects[rel] = data
        import shutil
        shutil.rmtree(self.base / ".r2cache", ignore_errors=True)
        shutil.rmtree(self.base / "thumbnails", ignore_errors=True)

    def tearDown(self):
        st = self.storage
        (st._ACCOUNT_ID, st._ACCESS_KEY, st._SECRET_KEY, st._BUCKET, st._SERVE,
         st._client, st._CACHE_MAX_BYTES, st._base_path, st._cache_dir) = self.saved_cfg
        st._size_cache.clear()

    def get(self, path, **kw):
        return self.client.get(path, follow_redirects=False, **kw)

    # -- public file route -------------------------------------------------------------------

    def test_file_in_r2_redirects_to_presigned_url(self):
        r = self.get("/api/documents/in-r2/file")
        self.assertEqual(r.status_code, 302)
        loc = urlparse(r.headers["location"])
        self.assertEqual(loc.netloc, R2_HOST)
        self.assertEqual(unquote(loc.path), f"/{BUCKET}/DOJ Disclosures/Data Set 1/EFTA00000100.pdf")
        q = parse_qs(loc.query)
        self.assertEqual(q["response-content-type"], ["application/pdf"])
        self.assertTrue(q["response-content-disposition"][0].startswith("inline"))
        self.assertIn("X-Amz-Signature", q)
        self.assertEqual(r.headers["cache-control"], "public, max-age=600")
        # The presigned URL actually fetches the object from (fake) R2.
        body = self.storage._http().get(r.headers["location"]).content
        self.assertEqual(body, self.r2.objects["DOJ Disclosures/Data Set 1/EFTA00000100.pdf"])

    def test_redirect_url_is_stable_within_the_signing_window(self):
        a = self.get("/api/documents/in-r2/file").headers["location"]
        b = self.get("/api/documents/in-r2/file").headers["location"]
        self.assertEqual(a, b)

    def test_object_missing_from_r2_falls_back_to_local_file(self):
        r = self.get("/api/documents/local-only/file")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.headers["content-type"], "application/pdf")

    def test_file_only_in_r2_streams_from_cache_when_serving_is_off(self):
        self.storage._SERVE = False
        r = self.get("/api/documents/r2-only/file")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.content, self.r2.objects["House Disclosures/memo with spaces.pdf"])
        self.assertTrue((self.base / ".r2cache/House Disclosures/memo with spaces.pdf").exists())

    def test_missing_everywhere_is_404(self):
        del self.r2.objects["House Disclosures/memo with spaces.pdf"]
        self.assertEqual(self.get("/api/documents/r2-only/file").status_code, 404)

    def test_r2_down_falls_back_to_local(self):
        self.r2.down = True
        self.assertEqual(self.get("/api/documents/in-r2/file").status_code, 200)

    def test_archived_version_is_never_presigned(self):
        r = self.get("/api/admin/documents/archived/file", headers={"x-api-key": ADMIN_KEY})
        self.assertEqual(r.status_code, 200)
        self.assertNotIn("location", r.headers)
        self.assertEqual(r.headers.get("cache-control"), "private, no-store")

    def test_admin_redirect_is_not_cacheable(self):
        r = self.get("/api/admin/documents/in-r2/file", headers={"x-api-key": ADMIN_KEY})
        self.assertEqual(r.status_code, 302)
        self.assertEqual(r.headers["cache-control"], "private, no-store")

    # -- features that need the file on disk -------------------------------------------------

    @unittest.skipIf(fitz is None, "PyMuPDF not installed")
    def test_pdf_find_works_for_file_only_in_r2(self):
        r = self.get("/api/documents/r2-only/pdf-find?t=Zurich")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["pages"], [{"page": 1, "hits": 1}])

    @unittest.skipIf(fitz is None, "PyMuPDF not installed")
    def test_thumbnail_generated_from_r2_copy(self):
        r = self.get("/api/documents/r2-only/thumbnail")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.headers["content-type"], "image/jpeg")

    def test_thumbnail_not_pinned_to_placeholder_while_r2_is_down(self):
        self.r2.down = True
        r = self.get("/api/documents/r2-only/thumbnail")
        self.assertEqual(r.status_code, 404)
        self.assertFalse((self.base / "thumbnails/r2-only.jpg").exists())

    # -- storage helpers ---------------------------------------------------------------------

    def test_restore_local_puts_file_back_at_its_real_path(self):
        p = self.base / "House Disclosures/memo with spaces.pdf"
        self.assertTrue(self.storage.restore_local(p))
        self.assertEqual(p.read_bytes(), self.r2.objects["House Disclosures/memo with spaces.pdf"])
        p.unlink()

    def test_upload_replaces_object_and_drops_stale_cache(self):
        rel = "DOJ Disclosures/Data Set 1/EFTA00000101.pdf"
        p = self.base / rel
        p.write_bytes(b"%PDF new content")
        self.assertTrue(self.storage.upload(p, "application/pdf"))
        self.assertEqual(self.r2.objects[rel], b"%PDF new content")

    def test_cache_is_trimmed_to_its_cap(self):
        st = self.storage
        st._CACHE_MAX_BYTES = 25
        for i in range(4):
            rel = f"CourtRecords/f{i}.pdf"
            self.r2.objects[rel] = b"x" * 10
            self.assertIsNotNone(st.ensure_local(self.base / rel))
            time.sleep(0.01)
        cached = sorted(p.name for p in (self.base / ".r2cache/CourtRecords").iterdir())
        self.assertLessEqual(len(cached), 2)
        self.assertIn("f3.pdf", cached)

    def test_ensure_local_respects_max_bytes(self):
        self.assertIsNone(self.storage.ensure_local(
            self.base / "House Disclosures/memo with spaces.pdf", max_bytes=5))

    def test_keys_outside_base_are_refused(self):
        self.assertIsNone(self.storage.key_for(self.base / ".." / "etc" / "passwd"))


if __name__ == "__main__":
    unittest.main()
