"""Tests for FTS5-backed friday/memory/search.py with scan fallback."""

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


class MemorySearchTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._env = patch.dict(
            os.environ, {"FRIDAY_KNOWLEDGE_DIR": self._tmp.name}, clear=False
        )
        self._env.start()
        self.vault_dir = Path(self._tmp.name) / "vault"
        (self.vault_dir / "Facts").mkdir(parents=True)
        (self.vault_dir / "Facts" / "coffee.md").write_text(
            "# Coffee preference\n\nThe boss drinks black coffee, no sugar, every morning.\n",
            encoding="utf-8",
        )
        (self.vault_dir / "Facts" / "tea.md").write_text(
            "# Tea\n\nGreen tea only when sick.\n", encoding="utf-8"
        )
        from friday.memory import index

        index.reindex(vault_dir=self.vault_dir, knowledge_dir=Path(self._tmp.name) / "nokn")

    def tearDown(self):
        self._env.stop()
        self._tmp.cleanup()

    def test_fts_search_finds_note_and_fills_hit_fields(self):
        from friday.memory import search

        hits = search.search("black coffee", k=5, root=self.vault_dir)
        self.assertTrue(hits)
        top = hits[0]
        self.assertEqual(top.path, "Facts/coffee.md")
        self.assertEqual(top.title, "Coffee preference")
        self.assertIn("coffee", top.snippet.lower())
        self.assertGreater(top.score, 0)

    def test_no_match_returns_empty(self):
        from friday.memory import search

        self.assertEqual(search.search("quantum chromodynamics", root=self.vault_dir), [])

    def test_scan_fallback_used_when_fts_unavailable(self):
        from friday.memory import search

        with patch.object(search, "_fts_search", side_effect=Exception("no fts")):
            hits = search.search("green tea", k=5, root=self.vault_dir)
        self.assertTrue(hits)
        self.assertEqual(hits[0].path, "Facts/tea.md")

    def test_weird_query_chars_do_not_crash(self):
        from friday.memory import search

        hits = search.search('coffee" OR 1=1 -- (*)', k=3, root=self.vault_dir)
        self.assertIsInstance(hits, list)


if __name__ == "__main__":
    unittest.main()
