"""Tests for friday/memory/reflect.py — LLM extraction is mocked."""

import asyncio
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch


class ReflectTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        # housekeeping (promotion/grooming) opens the memory DB — keep it
        # inside the tempdir, never the real knowledge folder
        self._env = patch.dict(
            os.environ, {"FRIDAY_KNOWLEDGE_DIR": self._tmp.name}, clear=False
        )
        self._env.start()
        self.vault_dir = Path(self._tmp.name) / "vault"

    def tearDown(self):
        self._env.stop()
        self._tmp.cleanup()

    def test_extracted_items_land_in_profile(self):
        from friday.memory import profile, reflect

        fake = AsyncMock(
            return_value={
                "items": [
                    {"type": "fact", "key": "occupation", "value": "founder"},
                    {"type": "preference", "key": "tone", "value": "casual"},
                    {"type": "fact", "key": "", "value": "ignored"},
                ]
            }
        )
        with patch.object(reflect, "_json_call", fake):
            saved = asyncio.run(
                reflect.run(
                    ["[user]: I'm a startup founder", "[assistant]: noted"],
                    root=self.vault_dir,
                )
            )
        self.assertEqual(len(saved), 2)
        self.assertEqual(profile.get_field("occupation", root=self.vault_dir), "founder")
        self.assertEqual(profile.get_field("tone", root=self.vault_dir), "casual")

    def test_llm_failure_is_silent(self):
        from friday.memory import reflect

        with patch.object(reflect, "_json_call", AsyncMock(side_effect=Exception("api down"))):
            saved = asyncio.run(reflect.run(["[user]: hi"], root=self.vault_dir))
        self.assertEqual(saved, [])


if __name__ == "__main__":
    unittest.main()
