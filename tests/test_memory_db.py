"""Tests for friday/memory/db.py — schema creation and env-pointed path."""

import os
import tempfile
import unittest
from unittest.mock import patch


class MemoryDbTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._env = patch.dict(
            os.environ, {"FRIDAY_KNOWLEDGE_DIR": self._tmp.name}, clear=False
        )
        self._env.start()

    def tearDown(self):
        self._env.stop()
        self._tmp.cleanup()

    def test_db_path_under_knowledge_dir(self):
        from friday.memory import db

        path = db.db_path()
        self.assertTrue(str(path).startswith(self._tmp.name))
        self.assertEqual(path.name, "memory.db")

    def test_connect_creates_schema(self):
        from friday.memory import db

        conn = db.connect()
        try:
            tables = {
                row[0]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type IN ('table','view')"
                )
            }
            self.assertIn("note_meta", tables)
            self.assertIn("facts", tables)
            self.assertIn("recall_log", tables)
            # FTS5 virtual table registers as 'notes_fts'
            self.assertIn("notes_fts", tables)
            # WAL mode is on
            mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
            self.assertEqual(mode.lower(), "wal")
        finally:
            conn.close()

    def test_connect_is_idempotent(self):
        from friday.memory import db

        c1 = db.connect()
        c1.close()
        c2 = db.connect()  # second connect must not fail on existing schema
        c2.execute("INSERT INTO note_meta (path, mtime) VALUES ('x', 1.0)")
        c2.commit()
        row = c2.execute("SELECT mtime FROM note_meta WHERE path = 'x'").fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(row["mtime"], 1.0)
        c2.close()


if __name__ == "__main__":
    unittest.main()
