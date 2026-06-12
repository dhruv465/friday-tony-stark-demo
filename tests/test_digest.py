"""Tests for friday/tools/digest.py — section assembly with mocked fetchers."""

import asyncio
import datetime as dt
import os
import tempfile
import unittest
from unittest.mock import AsyncMock, patch


class FakeMCP:
    def __init__(self):
        self.tools = {}

    def tool(self):
        def decorator(fn):
            self.tools[fn.__name__] = fn
            return fn

        return decorator


class DigestTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._env = patch.dict(
            os.environ, {"FRIDAY_KNOWLEDGE_DIR": self._tmp.name}, clear=False
        )
        self._env.start()

    def tearDown(self):
        self._env.stop()
        self._tmp.cleanup()

    def _register(self):
        from friday.tools import digest

        mcp = FakeMCP()
        digest.register(mcp)
        return mcp

    def test_digest_assembles_sections_and_caches(self):
        from friday.memory import facts
        from friday.tools import digest as digest_mod

        facts.add_event("standup", dt.datetime.now() + dt.timedelta(hours=3))
        mcp = self._register()
        with patch.object(
            digest_mod, "gather_world_news", AsyncMock(return_value="BRIEFING (LIVE)\n1. Big story")
        ), patch.object(
            digest_mod, "gather_finance_news", AsyncMock(return_value="BRIEFING (LIVE)\n1. Markets up")
        ), patch.object(
            digest_mod, "_weather", AsyncMock(return_value="Sunny, 31°C in Bengaluru")
        ):
            out = asyncio.run(mcp.tools["morning_digest"]())
            self.assertIn("standup", out)
            self.assertIn("Big story", out)
            self.assertIn("Markets up", out)
            self.assertIn("Sunny", out)
            # cached: second call returns same content without re-fetching
            out2 = asyncio.run(mcp.tools["morning_digest"]())
            self.assertEqual(out, out2)

    def test_fresh_regenerates_and_failed_sections_degrade(self):
        from friday.tools import digest as digest_mod

        mcp = self._register()
        with patch.object(
            digest_mod, "gather_world_news", AsyncMock(side_effect=Exception("net down"))
        ), patch.object(
            digest_mod, "gather_finance_news", AsyncMock(return_value="BRIEFING (LIVE)\n1. Calm")
        ), patch.object(
            digest_mod, "_weather", AsyncMock(side_effect=Exception("api down"))
        ):
            out = asyncio.run(mcp.tools["morning_digest"](fresh=True))
            self.assertIn("Calm", out)
            self.assertIn("DIGEST", out)


if __name__ == "__main__":
    unittest.main()
