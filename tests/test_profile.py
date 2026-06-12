"""Tests for friday/memory/profile.py."""

import tempfile
import unittest
from pathlib import Path


class ProfileTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.vault_dir = Path(self._tmp.name) / "vault"

    def tearDown(self):
        self._tmp.cleanup()

    def test_update_field_creates_then_replaces(self):
        from friday.memory import profile

        profile.update_field("address_as", "boss", root=self.vault_dir)
        profile.update_field("address_as", "chief", root=self.vault_dir)
        profile.update_field("tone", "dry", root=self.vault_dir)
        text = profile.profile_text(root=self.vault_dir)
        self.assertIn("- **address_as**: chief", text)
        self.assertNotIn("boss", text)
        self.assertIn("- **tone**: dry", text)
        raw = (self.vault_dir / "Profile" / "about_user.md").read_text(encoding="utf-8")
        self.assertIn("tags: [profile]", raw)

    def test_profile_text_empty_when_missing(self):
        from friday.memory import profile

        self.assertEqual(profile.profile_text(root=self.vault_dir), "")

    def test_get_field(self):
        from friday.memory import profile

        profile.update_field("use_emojis", "no", root=self.vault_dir)
        self.assertEqual(profile.get_field("use_emojis", root=self.vault_dir), "no")
        self.assertIsNone(profile.get_field("nope", root=self.vault_dir))

    def test_update_field_ignores_empty_input(self):
        from friday.memory import profile

        profile.update_field("", "x", root=self.vault_dir)
        profile.update_field("field", "  ", root=self.vault_dir)
        self.assertEqual(profile.profile_text(root=self.vault_dir), "")

    def test_update_field_collapses_newlines(self):
        from friday.memory import profile

        profile.update_field("occu\npation", "found\ner", root=self.vault_dir)
        self.assertEqual(
            profile.get_field("occu pation", root=self.vault_dir), "found er"
        )


if __name__ == "__main__":
    unittest.main()
