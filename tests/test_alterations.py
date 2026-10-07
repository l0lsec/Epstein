"""Tests for backend/alterations.py. Run: python -m unittest discover -s tests"""

import os
import sqlite3
import sys
import tempfile
import time
import types
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "backend"))

from alterations import analyze_versions  # noqa: E402

BASE = (
    "On March 3 the committee met with Jeffrey Smith and Maria Alvarez at the office.\n"
    "They discussed the transfer of funds to the account held in Zurich.\n"
    "Contact: smith.j@example.com or 212-555-0147 for the schedule.\n"
    "The meeting adjourned after two hours and nothing further was recorded."
)


class ReflowAndNoise(unittest.TestCase):
    def test_reflowed_text_is_identical(self):
        reflowed = BASE.replace("\n", " ").replace("the account held", "the account\nheld")
        a = analyze_versions(BASE, reflowed)
        self.assertEqual(a.kind, "identical")
        self.assertEqual((a.lines_removed, a.words_removed), (0, 0))
        self.assertTrue(a.is_trivial)
        self.assertEqual(a.severity, 0)

    def test_case_whitespace_and_zero_width_are_ignored(self):
        noisy = BASE.upper().replace(" ", "  ").replace("Jeffrey", "Jef​frey".upper())
        self.assertEqual(analyze_versions(BASE, noisy).kind, "identical")

    def test_ocr_spelling_variants_are_cosmetic(self):
        ocr = BASE.replace("committee", "comrnittee").replace("Zurich", "Zurlch")
        a = analyze_versions(BASE, ocr)
        self.assertEqual(a.kind, "cosmetic")
        self.assertGreaterEqual(a.ocr_variants, 1)
        self.assertEqual(a.significant_removed, 0)

    def test_page_furniture_changes_are_cosmetic(self):
        old = BASE + "\n\nPage 1 of 2\nEFTA00012345"
        new = BASE + "\n\nPage 1 of 3\nEFTA00099999"
        self.assertEqual(analyze_versions(old, new).kind, "cosmetic")

    def test_numbers_never_pair_as_ocr_noise(self):
        a = analyze_versions("account 1234567 opened", "account 1234568 opened")
        self.assertNotIn(a.kind, ("identical", "cosmetic"))


class Substantive(unittest.TestCase):
    def test_surgical_name_removal(self):
        new = BASE.replace("Jeffrey Smith and ", "")
        a = analyze_versions(BASE, new)
        self.assertEqual(a.kind, "removal")
        self.assertEqual(a.lines_removed, 1)
        self.assertEqual(a.names_removed, 2)
        self.assertIn("smith", a.removed_terms)
        self.assertGreater(a.similarity, 0.9)
        self.assertGreater(a.severity, 0)

    def test_redaction_mark_replacing_text(self):
        a = analyze_versions(BASE, BASE.replace("Jeffrey Smith", "[REDACTED]"))
        self.assertEqual(a.kind, "redaction")
        self.assertEqual(a.redactions_added, 1)
        self.assertEqual(a.names_removed, 2)

    def test_redaction_mark_added_over_unchanged_text(self):
        # Text layer left intact beneath a new black box: still a redaction.
        a = analyze_versions(BASE, BASE + "\n██████████ (b)(6)")
        self.assertEqual(a.kind, "redaction")
        self.assertEqual(a.redactions_added, 2)
        self.assertEqual(a.significant_removed, 0)

    def test_preexisting_marks_do_not_count(self):
        old = BASE + "\n[REDACTED]"
        a = analyze_versions(old, old.replace("Zurich", "Geneva"))
        self.assertEqual(a.redactions_added, 0)

    def test_removed_identifier_is_substantive(self):
        a = analyze_versions(BASE, BASE.replace("212-555-0147", ""))
        self.assertEqual(a.identifiers_removed, 1)
        self.assertEqual(a.kind, "removal")

    def test_dropped_page_is_substantive(self):
        a = analyze_versions(BASE, BASE, old_pages=5, new_pages=4)
        self.assertEqual(a.pages_removed, 1)
        self.assertEqual(a.kind, "removal")

    def test_wholesale_replacement(self):
        new = (
            "Quarterly maintenance schedule for the facility boiler covers heating units, "
            "ventilation ducts, sprinkler valves, elevator cables, generator batteries, "
            "roofing membranes, parking lighting, security cameras, fire extinguishers, "
            "plumbing fixtures, carpet cleaning and window washing across every building."
        )
        self.assertEqual(analyze_versions(BASE, new).kind, "replaced")

    def test_additions_only_are_not_a_suppression(self):
        a = analyze_versions(BASE, BASE + "\nAn appendix listing exhibits was attached later.")
        self.assertEqual(a.kind, "additive")
        self.assertTrue(a.is_trivial)

    def test_no_text_on_either_side(self):
        for old, new in ((None, None), ("", "   \n"), ("\x0c", "")):
            self.assertEqual(analyze_versions(old, new).kind, "no_text")

    def test_everything_removed(self):
        a = analyze_versions(BASE, "")
        self.assertEqual(a.kind, "removal")
        self.assertEqual(a.similarity, 0.0)

    def test_severity_orders_by_evidence(self):
        cosmetic = analyze_versions(BASE, BASE.replace("\n", " "))
        small = analyze_versions(BASE, BASE.replace("Zurich", "somewhere"))
        marked = analyze_versions(BASE, BASE.replace("Jeffrey Smith", "[REDACTED]"))
        self.assertEqual(cosmetic.severity, 0)
        self.assertLess(small.severity, marked.severity)
        self.assertLessEqual(marked.severity, 100)


class Performance(unittest.TestCase):
    def test_large_document_stays_fast(self):
        words = [f"word{i % 5000}" for i in range(150_000)]
        old = " ".join(words)
        new = " ".join(words[:70_000] + words[70_050:])
        t0 = time.time()
        a = analyze_versions(old, new)
        self.assertLess(time.time() - t0, 10)
        self.assertEqual(a.kind, "removal")


class StoredQueue(unittest.TestCase):
    """Database.analyze_alterations: re-scores rows without touching admin decisions."""

    def setUp(self):
        # database.py only needs extract_email_date from the extractor; stub it when the
        # PDF stack (pdfplumber, tqdm...) isn't installed so these tests run anywhere.
        try:
            import extractor  # noqa: F401
        except ImportError:
            stub = types.ModuleType("extractor")
            stub.extract_email_date = lambda *a, **k: None
            sys.modules["extractor"] = stub
        from database import Database
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Database(os.path.join(self.tmp.name, "t.db"))

    def tearDown(self):
        self.tmp.cleanup()

    def _doc(self, conn, doc_id, filename, text, pages=1):
        conn.execute(
            "INSERT INTO documents (id, filename, path, category, subcategory, file_type, "
            "page_count, char_count, full_text) VALUES (?,?,?,?,?,?,?,?,?)",
            (doc_id, filename, filename, "DOJ", "ds1", "pdf", pages, len(text), text),
        )

    def _alt(self, conn, efta, old_id, new_id, status, reviewed=False, lines_removed=0):
        conn.execute(
            "INSERT INTO document_alterations (efta_num, file_type, dataset_num, canonical_id, "
            "canonical_filename, old_id, old_filename, lines_removed, review_status, reviewed_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            (efta, "pdf", 1, new_id, f"EFTA{efta:08d}.pdf", old_id, "old.pdf", lines_removed, status,
             "2026-01-01" if reviewed else None),
        )

    def test_rescoring_respects_admin_decisions(self):
        reflow = BASE.replace("\n", " ")
        with self.db.get_connection() as conn:
            for i, (old, new) in enumerate([
                (BASE, reflow),                                   # 1: reflow noise, auto-pending
                (BASE, BASE.replace("Jeffrey Smith", "[REDACTED]")),  # 2: real, auto-trivial (missed)
                (BASE, reflow),                                   # 3: reflow, admin cleared
                (BASE, reflow),                                   # 4: reflow, admin set trivial
            ], start=1):
                self._doc(conn, f"o{i}", f"EFTA{i:08d}_20260101_000000.pdf", old)
                self._doc(conn, f"n{i}", f"EFTA{i:08d}.pdf", new)
            self._alt(conn, 1, "o1", "n1", "pending", lines_removed=3)
            self._alt(conn, 2, "o2", "n2", "trivial")
            self._alt(conn, 3, "o3", "n3", "cleared", reviewed=True)
            self._alt(conn, 4, "o4", "n4", "pending", reviewed=True)
            conn.commit()

        def status():
            with self.db.get_read_connection() as conn:
                return {r[0]: r[1] for r in conn.execute(
                    "SELECT efta_num, review_status FROM document_alterations")}

        dry = self.db.analyze_alterations(apply=False)
        self.assertEqual(status(), {1: "pending", 2: "trivial", 3: "cleared", 4: "pending"})
        self.assertEqual((dry["to_trivial"], dry["to_pending"], dry["left_alone"]), (1, 1, 2))

        self.db.analyze_alterations(apply=True)
        self.assertEqual(status(), {1: "trivial", 2: "pending", 3: "cleared", 4: "pending"})
        with self.db.get_read_connection() as conn:
            row = conn.execute(
                "SELECT change_kind, severity, redactions_added, lines_removed "
                "FROM document_alterations WHERE efta_num = 2").fetchone()
            self.assertEqual((row[0], row[2]), ("redaction", 1))
            self.assertGreater(row[1], 0)
            self.assertGreater(row[3], 0)
            # Admin-reviewed rows keep their original counts; only analysis columns refresh.
            row3 = conn.execute(
                "SELECT change_kind, lines_removed FROM document_alterations WHERE efta_num = 3").fetchone()
            self.assertEqual(row3[0], "identical")

    def test_rows_without_a_version_are_skipped(self):
        with self.db.get_connection() as conn:
            self._doc(conn, "n9", "EFTA00000009.pdf", BASE)
            self._alt(conn, 9, None, "n9", "pending")
            conn.commit()
        report = self.db.analyze_alterations(apply=True)
        self.assertEqual((report["analyzed"], report["skipped"]), (0, 1))


if __name__ == "__main__":
    unittest.main()
