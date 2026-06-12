"""Tests for the event-alert sweep in friday/agents/runtime.py."""

import datetime as dt
import os
import tempfile
import unittest
from unittest.mock import patch


class EventAlertTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._env = patch.dict(
            os.environ,
            {
                "FRIDAY_KNOWLEDGE_DIR": self._tmp.name,
                "FRIDAY_DESKTOP_EVENT_LOG": os.path.join(self._tmp.name, "events.jsonl"),
            },
            clear=False,
        )
        self._env.start()

    def tearDown(self):
        self._env.stop()
        self._tmp.cleanup()

    def test_due_event_notifies_once(self):
        from friday.agents import runtime
        from friday.memory import facts

        start = dt.datetime.now() + dt.timedelta(minutes=15)
        facts.add_event("investor call", start)

        with patch.object(runtime, "_mac_notification") as mac, patch.object(
            runtime, "set_pending_notify"
        ) as pending:
            runtime.check_event_alerts()
            self.assertEqual(mac.call_count, 1)
            self.assertEqual(pending.call_count, 1)
            detail = pending.call_args[0][1]
            self.assertIn("investor call", detail)
            self.assertIn("15", detail)
            # second sweep: window already marked, nothing fires
            runtime.check_event_alerts()
            self.assertEqual(mac.call_count, 1)

    def test_alert_phrasing_per_window(self):
        from friday.agents import runtime

        self.assertIn("starting now", runtime._alert_line({"content": "x", "window": "now", "minutes_until": 0.0}))
        self.assertIn("in about an hour", runtime._alert_line({"content": "x", "window": "1h", "minutes_until": 55.0}))
        self.assertIn("in 12 minutes", runtime._alert_line({"content": "x", "window": "15m", "minutes_until": 12.4}))

    def test_sweep_never_raises(self):
        from friday.agents import runtime

        with patch("friday.memory.facts.events_due_for_alert", side_effect=Exception("db gone")):
            runtime.check_event_alerts()  # must not raise


if __name__ == "__main__":
    unittest.main()
