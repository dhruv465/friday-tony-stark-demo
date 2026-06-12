"""Tests for friday/memory/context.py — the per-turn injection block."""

import datetime as dt
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch


class ContextBlockTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._env = patch.dict(
            os.environ, {"FRIDAY_KNOWLEDGE_DIR": self._tmp.name}, clear=False
        )
        self._env.start()
        self._vault_tmp = tempfile.TemporaryDirectory()
        self.vault_dir = Path(self._vault_tmp.name)
        (self.vault_dir / "Facts").mkdir(parents=True)
        # freeze the search module's reindex throttle so build_block never
        # walks the real vault via a default reindex
        from friday.memory import index

        index._last_reindex_ts = time.time()

    def tearDown(self):
        self._env.stop()
        self._vault_tmp.cleanup()
        self._tmp.cleanup()

    def test_block_always_carries_current_time(self):
        from friday.memory import context

        block = context.build_block("hello", root=self.vault_dir)
        self.assertIn("Current time:", block)
        self.assertIn("internal awareness", block)

    def test_block_includes_profile_and_events_and_memory(self):
        from friday.memory import context, facts, index, profile

        profile.update_field("address_as", "Chief", root=self.vault_dir)
        facts.add_event("dentist", dt.datetime.now() + dt.timedelta(days=1))
        (self.vault_dir / "Facts" / "coffee.md").write_text(
            "# Coffee\n\nBlack coffee, no sugar.\n", encoding="utf-8"
        )
        index.reindex(vault_dir=self.vault_dir, knowledge_dir=Path(self._tmp.name) / "nokn")

        block = context.build_block("what coffee do I drink", root=self.vault_dir)
        self.assertIn("address_as", block)
        self.assertIn("TOMORROW", block)
        self.assertIn("coffee", block.lower())

    def test_due_reminder_is_marked_sent(self):
        from friday.memory import context, facts

        facts.add_event("flight", dt.datetime.now() + dt.timedelta(hours=30))
        block = context.build_block("hey", root=self.vault_dir)
        self.assertIn("REMINDER", block)
        block2 = context.build_block("hey again", root=self.vault_dir)
        self.assertNotIn("REMINDER", block2)

    def test_never_raises(self):
        from friday.memory import context

        with patch("friday.memory.search.search", side_effect=Exception("boom")):
            block = context.build_block("anything", root=self.vault_dir)
        self.assertIsInstance(block, str)


if __name__ == "__main__":
    unittest.main()
