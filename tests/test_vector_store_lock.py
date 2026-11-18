"""Concurrent first searches must load the embeddings matrix and the model only once."""
import pickle
import sys
import tempfile
import threading
import time
import types
import unittest
from pathlib import Path
from unittest import mock

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend"))
from database import VectorStore  # noqa: E402


class VectorStoreLockTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        d = Path(self.tmp.name)
        with open(d / "embeddings.pkl", "wb") as f:
            pickle.dump([np.ones(4, dtype=np.float32) * i for i in range(10)], f)
        with open(d / "metadata.pkl", "wb") as f:
            pickle.dump([{"doc_id": f"d{i}"} for i in range(10)], f)
        self.store = VectorStore(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def _run_concurrently(self, fn, n=8):
        start = threading.Barrier(n)
        out = []

        def go():
            start.wait()
            out.append(fn())

        threads = [threading.Thread(target=go) for _ in range(n)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        return out

    def test_matrix_loads_once_under_concurrency(self):
        real_load = pickle.load
        calls = []

        def slow_load(f):
            calls.append(1)
            time.sleep(0.2)  # wide window for the race
            return real_load(f)

        with mock.patch("database.pickle.load", side_effect=slow_load):
            results = self._run_concurrently(self.store._ensure_matrix)
        self.assertEqual(results, [True] * 8)
        self.assertEqual(len(calls), 1)
        self.assertEqual(self.store._embeddings_matrix.shape, (10, 4))

    def test_model_loads_once_under_concurrency(self):
        created = []

        class FakeModel:
            def __init__(self, name, device=None):
                created.append(name)
                time.sleep(0.2)

        fake_st = types.SimpleNamespace(SentenceTransformer=FakeModel)
        fake_torch = types.SimpleNamespace(
            backends=types.SimpleNamespace(mps=types.SimpleNamespace(is_available=lambda: False)),
            cuda=types.SimpleNamespace(is_available=lambda: False),
        )
        with mock.patch.dict(sys.modules, {"sentence_transformers": fake_st, "torch": fake_torch}):
            results = self._run_concurrently(self.store._get_model)
        self.assertEqual(len(created), 1)
        self.assertTrue(all(r is results[0] for r in results))


if __name__ == "__main__":
    unittest.main()
