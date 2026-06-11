import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from friday.learning import store


class LearningStoreTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._env = patch.dict(
            os.environ, {"FRIDAY_KNOWLEDGE_DIR": self._tmp.name}, clear=False
        )
        self._env.start()

    def tearDown(self):
        self._env.stop()
        self._tmp.cleanup()

    def test_topic_dir_rejects_escape(self):
        with self.assertRaises(store.KnowledgeError):
            store.topic_dir("../evil")
        with self.assertRaises(store.KnowledgeError):
            store.topic_dir("nested/extra")

    def test_job_roundtrip_atomic(self):
        job = store.new_job("Rust Async", "rust-async")
        store.save_job("rust-async", job)

        loaded = store.load_job("rust-async")
        self.assertIsNotNone(loaded)
        self.assertEqual(loaded["topic"], "Rust Async")
        self.assertEqual(loaded["status"], "running")
        self.assertEqual(loaded["coverage"], "shallow")
        tmp_leftover = store.topic_dir("rust-async") / "job.json.tmp"
        self.assertFalse(tmp_leftover.exists())

    def test_write_and_read_docs_with_frontmatter(self):
        store.write_doc(
            "rust-async",
            "index.md",
            "# Rust Async\n\nNotes body.",
            frontmatter={"topic": "Rust Async", "coverage": "developing"},
        )
        body = store.read_doc("rust-async", "index.md")
        self.assertIn("---", body)
        self.assertIn("coverage: developing", body)
        self.assertIn("Notes body.", body)
        with self.assertRaises(store.KnowledgeError):
            store.write_doc("rust-async", "../outside.md", "nope")
        with self.assertRaises(store.KnowledgeError):
            store.write_doc("rust-async", "job.json", "nope")

    def test_runner_lock_acquire_release_and_stale_takeover(self):
        slug = "rust-async"
        self.assertTrue(store.acquire_runner_lock(slug))
        # Same pid can re-acquire (in-process duplicate guard lives in the runtime).
        self.assertTrue(store.acquire_runner_lock(slug))

        # A live lock held by another (running) pid blocks acquisition.
        lock_path = store.topic_dir(slug) / store.LOCK_NAME
        lock_path.write_text(
            json.dumps({"pid": os.getppid(), "heartbeat": time.time()}),
            encoding="utf-8",
        )
        self.assertFalse(store.acquire_runner_lock(slug))

        # A stale heartbeat is taken over even if the pid exists.
        lock_path.write_text(
            json.dumps(
                {"pid": os.getppid(), "heartbeat": time.time() - store.LOCK_STALE_S - 5}
            ),
            encoding="utf-8",
        )
        self.assertTrue(store.acquire_runner_lock(slug))

        store.release_runner_lock(slug)
        self.assertFalse(lock_path.exists())

    def test_recall_finds_topic_notes(self):
        store.write_doc(
            "zeta-widgets",
            "index.md",
            "# Zeta Widgets\n\nZeta widgets are flux-coupled gizmos.",
        )
        hits = store.recall("zeta widgets")
        self.assertTrue(hits)
        self.assertEqual(hits[0].path, "zeta-widgets/index.md")
        # Hits stay inside the knowledge root, not the memory vault.
        self.assertTrue(
            (Path(self._tmp.name) / "zeta-widgets" / "index.md").exists()
        )

    def test_known_topics_lists_jobs(self):
        store.save_job("rust-async", store.new_job("Rust Async", "rust-async"))
        store.save_job("zeta-widgets", store.new_job("Zeta Widgets", "zeta-widgets"))
        topics = store.known_topics()
        self.assertEqual(len(topics), 2)
        self.assertEqual(
            {t["slug"] for t in topics}, {"rust-async", "zeta-widgets"}
        )
        self.assertIn("coverage", topics[0])


if __name__ == "__main__":
    unittest.main()
