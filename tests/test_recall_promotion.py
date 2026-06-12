"""Tests for friday/memory/recall_log.py — tracking + promotion."""

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


class RecallPromotionTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._env = patch.dict(
            os.environ, {"FRIDAY_KNOWLEDGE_DIR": self._tmp.name}, clear=False
        )
        self._env.start()

    def tearDown(self):
        self._env.stop()
        self._tmp.cleanup()

    def _hit(self, path="Facts/coffee.md", score=2.0):
        from friday.memory.search import Hit

        return Hit(path=path, title="Coffee", score=score, snippet="boss drinks black coffee")

    def test_track_inserts_then_increments(self):
        from friday.memory import db, recall_log

        recall_log.track([self._hit()], "what coffee does he like")
        recall_log.track([self._hit()], "morning drink preference")
        conn = db.connect()
        try:
            row = conn.execute("SELECT * FROM recall_log").fetchone()
        finally:
            conn.close()
        self.assertEqual(row["recall_count"], 2)
        import json

        self.assertEqual(len(json.loads(row["query_hashes"])), 2)

    def test_evaluate_scores_in_unit_range_and_needs_recalls(self):
        from friday.memory.recall_log import evaluate_candidate

        empty = evaluate_candidate(
            {"recall_count": 0, "total_score": 0, "query_hashes": [], "recall_days": [], "concept_tags": []}
        )
        self.assertFalse(empty["valid"])
        strong = evaluate_candidate(
            {
                "recall_count": 8,
                "total_score": 8.0,
                "query_hashes": ["a", "b", "c", "d", "e"],
                "recall_days": ["2026-06-01", "2026-06-03", "2026-06-05"],
                "concept_tags": ["coffee", "morning", "preference"],
            }
        )
        self.assertTrue(strong["valid"])
        self.assertGreaterEqual(strong["score"], 0.55)
        self.assertLessEqual(strong["score"], 1.0)

    def test_promote_writes_vault_note_and_marks_row(self):
        from friday.memory import db, recall_log

        vault_dir = Path(self._tmp.name) / "vault"
        for i in range(8):
            recall_log.track([self._hit()], f"distinct query number {i}")
        # spread recall_days artificially so consolidation scores
        conn = db.connect()
        conn.execute(
            "UPDATE recall_log SET recall_days = ?",
            ('["2026-06-01","2026-06-03","2026-06-05"]',),
        )
        conn.commit()
        conn.close()
        promoted = recall_log.promote(vault_dir=vault_dir)
        self.assertEqual(len(promoted), 1)
        note = vault_dir / "Profile" / "promoted.md"
        self.assertTrue(note.exists())
        self.assertIn("black coffee", note.read_text(encoding="utf-8"))
        # second promote run: nothing left
        self.assertEqual(recall_log.promote(vault_dir=vault_dir), [])


if __name__ == "__main__":
    unittest.main()
