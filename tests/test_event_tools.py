"""Tests for friday/tools/events.py MCP tools."""

import asyncio
import datetime as dt
import os
import tempfile
import unittest
from unittest.mock import patch


class FakeMCP:
    def __init__(self):
        self.tools = {}

    def tool(self):
        def decorator(fn):
            self.tools[fn.__name__] = fn
            return fn

        return decorator


class EventToolsTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._env = patch.dict(
            os.environ, {"FRIDAY_KNOWLEDGE_DIR": self._tmp.name}, clear=False
        )
        self._env.start()
        from friday.tools import events

        self.mcp = FakeMCP()
        events.register(self.mcp)

    def tearDown(self):
        self._env.stop()
        self._tmp.cleanup()

    def _run(self, name, **kwargs):
        return asyncio.run(self.mcp.tools[name](**kwargs))

    def test_add_event_with_iso_start(self):
        start = (dt.datetime.now() + dt.timedelta(days=1)).isoformat(timespec="minutes")
        out = self._run("add_event", content="dentist", start=start)
        self.assertIn("dentist", out)
        listing = self._run("upcoming_events")
        self.assertIn("dentist", listing)
        self.assertIn("TOMORROW", listing)

    def test_add_event_rejects_bad_datetime(self):
        out = self._run("add_event", content="x", start="next tuesday-ish")
        self.assertIn("ISO", out)

    def test_cancel_event(self):
        start = (dt.datetime.now() + dt.timedelta(days=1)).isoformat(timespec="minutes")
        self._run("add_event", content="dentist", start=start)
        out = self._run("cancel_event", keyword="dentist")
        self.assertIn("1", out)
        self.assertIn("no upcoming events", self._run("upcoming_events").lower())


if __name__ == "__main__":
    unittest.main()
