"""Tests for friday/memory/feedback.py — instant preference regexes."""

import tempfile
import unittest
from pathlib import Path


class FeedbackTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.vault_dir = Path(self._tmp.name) / "vault"

    def tearDown(self):
        self._tmp.cleanup()

    def _detect(self, text):
        from friday.memory import feedback

        return feedback.detect_and_save(text, root=self.vault_dir)

    def test_call_me(self):
        from friday.memory import profile

        saved = self._detect("From now on call me Chief")
        self.assertIn(("address_as", "Chief"), saved)
        self.assertEqual(profile.get_field("address_as", root=self.vault_dir), "Chief")

    def test_style_and_emoji(self):
        self.assertIn(("response_style", "concise"), self._detect("be more concise please"))
        self.assertIn(("use_emojis", "no"), self._detect("don't use emojis"))

        from friday.memory import profile

        # the bare "use emojis" pattern must NOT overwrite the negative match
        self.assertEqual(profile.get_field("use_emojis", root=self.vault_dir), "no")

    def test_no_match_saves_nothing(self):
        self.assertEqual(self._detect("what's the weather like"), [])

    def test_idempotent(self):
        self._detect("call me Chief")
        self.assertEqual(self._detect("call me Chief"), [])


if __name__ == "__main__":
    unittest.main()
