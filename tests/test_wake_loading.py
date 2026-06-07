import unittest
from unittest.mock import patch

import agent_friday


class WakeLoadingTests(unittest.TestCase):
    def test_wake_phrase_emits_loading_state_before_agent_reply(self):
        events = []

        with (
            patch("friday.desktop.launcher.ensure_desktop_running", return_value={"status": "running"}),
            patch("agent_friday.append_event", side_effect=lambda event_type, **payload: events.append((event_type, payload))),
        ):
            agent_friday._handle_wake_phrase("wake up")

        self.assertGreaterEqual(len(events), 2)
        self.assertEqual(events[0][0], "state")
        self.assertEqual(events[0][1]["state"], "thinking")
        self.assertEqual(events[1][0], "activity")
        self.assertEqual(events[1][1]["tool"], "wake")
        self.assertIn("loading", events[1][1]["detail"])

    def test_wake_phrase_launches_desktop_in_loading_mode(self):
        with (
            patch("friday.desktop.launcher.ensure_desktop_running", return_value={"status": "launched"}) as ensure,
            patch("agent_friday.append_event"),
        ):
            agent_friday._handle_wake_phrase("wake up daddy's home")

        ensure.assert_called_once_with(wake_loading=True)


if __name__ == "__main__":
    unittest.main()
