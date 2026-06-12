"""Tests for friday/memory/index.py — incremental FTS reindexing."""

import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch


class MemoryIndexTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._env = patch.dict(
            os.environ, {"FRIDAY_KNOWLEDGE_DIR": self._tmp.name}, clear=False
        )
        self._env.start()
        self.vault_dir = Path(self._tmp.name) / "vault"
        self.knowledge_dir = Path(self._tmp.name) / "knowledge"
        self.vault_dir.mkdir()
        self.knowledge_dir.mkdir()
        (self.vault_dir / "Facts").mkdir()
        (self.vault_dir / "Facts" / "coffee.md").write_text(
            "# Coffee preference\n\nThe boss drinks black coffee, no sugar.\n",
            encoding="utf-8",
        )
        (self.knowledge_dir / "topic.md").write_text(
            "# Vector databases\n\nHNSW is an index structure.\n", encoding="utf-8"
        )
        # underscore dirs must be skipped
        hidden = self.knowledge_dir / "_agents"
        hidden.mkdir()
        (hidden / "report.md").write_text("# secret agent report\n", encoding="utf-8")

    def tearDown(self):
        self._env.stop()
        self._tmp.cleanup()

    def _reindex(self):
        from friday.memory import index

        return index.reindex(
            vault_dir=self.vault_dir, knowledge_dir=self.knowledge_dir
        )

    def _fts_paths(self):
        from friday.memory import db

        conn = db.connect()
        try:
            return {r["path"] for r in conn.execute("SELECT path FROM notes_fts")}
        finally:
            conn.close()

    def test_first_reindex_indexes_all_visible_notes(self):
        stats = self._reindex()
        self.assertEqual(stats["indexed"], 2)
        paths = self._fts_paths()
        self.assertIn("vault:Facts/coffee.md", paths)
        self.assertIn("knowledge:topic.md", paths)
        self.assertNotIn("knowledge:_agents/report.md", paths)

    def test_unchanged_files_are_skipped_on_second_pass(self):
        self._reindex()
        stats = self._reindex()
        self.assertEqual(stats["indexed"], 0)
        self.assertEqual(stats["removed"], 0)

    def test_modified_file_is_reindexed_and_deleted_file_removed(self):
        self._reindex()
        note = self.vault_dir / "Facts" / "coffee.md"
        time.sleep(0.01)
        note.write_text("# Coffee preference\n\nSwitched to espresso.\n", encoding="utf-8")
        os.utime(note, (time.time() + 5, time.time() + 5))
        (self.knowledge_dir / "topic.md").unlink()
        stats = self._reindex()
        self.assertEqual(stats["indexed"], 1)
        self.assertEqual(stats["removed"], 1)
        paths = self._fts_paths()
        self.assertNotIn("knowledge:topic.md", paths)


if __name__ == "__main__":
    unittest.main()
