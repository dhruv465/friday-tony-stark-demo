"""Tests for friday/memory/facts.py — temporal events, conflicts, alerts."""

import datetime as dt
import os
import tempfile
import unittest
from unittest.mock import patch


class FactsStoreTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._env = patch.dict(
            os.environ, {"FRIDAY_KNOWLEDGE_DIR": self._tmp.name}, clear=False
        )
        self._env.start()

    def tearDown(self):
        self._env.stop()
        self._tmp.cleanup()

    def _add(self, content, start, hours=1.0, importance=0.5):
        from friday.memory import facts

        return facts.add_event(
            content, start, start + dt.timedelta(hours=hours), importance=importance
        )

    def test_add_and_list_active_sorted(self):
        from friday.memory import facts

        now = dt.datetime.now()
        self._add("dentist", now + dt.timedelta(days=2))
        self._add("standup", now + dt.timedelta(days=1))
        active = facts.get_active_events()
        self.assertEqual([f["content"] for f in active], ["standup", "dentist"])

    def test_cancel_by_keyword(self):
        from friday.memory import facts

        now = dt.datetime.now()
        self._add("dentist appointment", now + dt.timedelta(days=2))
        removed = facts.cancel_event("dentist")
        self.assertEqual(removed, 1)
        self.assertEqual(facts.get_active_events(), [])

    def test_overlap_marks_both_contested(self):
        from friday.memory import facts

        start = dt.datetime.now() + dt.timedelta(days=1)
        self._add("meeting A", start, hours=2)
        self._add("meeting B", start + dt.timedelta(minutes=30), hours=2)
        facts.lint_conflicts()
        contested = facts.get_contested_events()
        self.assertEqual(len(contested), 2)

    def test_day_before_reminder_window_fires_once(self):
        from friday.memory import facts

        start = dt.datetime.now() + dt.timedelta(hours=30)
        self._add("flight to Delhi", start)
        due = facts.get_events_needing_reminder()
        self.assertEqual(len(due), 1)
        facts.mark_reminder_sent(due[0]["id"])
        self.assertEqual(facts.get_events_needing_reminder(), [])

    def test_alert_windows_fire_once_each(self):
        from friday.memory import facts

        now = dt.datetime.now()
        self._add("call with investor", now + dt.timedelta(minutes=55))
        self._add("gym", now + dt.timedelta(minutes=15))
        self._add("launch", now + dt.timedelta(minutes=1))
        due = facts.events_due_for_alert(now=now)
        windows = {(d["content"], d["window"]) for d in due}
        self.assertEqual(
            windows,
            {("call with investor", "1h"), ("gym", "15m"), ("launch", "now")},
        )
        for d in due:
            facts.mark_alerted(d["id"], d["window"])
        self.assertEqual(facts.events_due_for_alert(now=now), [])

    def test_groom_expires_past_events(self):
        from friday.memory import facts

        past = dt.datetime.now() - dt.timedelta(days=2)
        self._add("yesterday thing", past)
        expired = facts.groom_expired()
        self.assertEqual(expired, 1)
        self.assertEqual(facts.get_active_events(), [])


if __name__ == "__main__":
    unittest.main()
