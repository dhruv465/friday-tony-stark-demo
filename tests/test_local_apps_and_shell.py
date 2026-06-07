import os
import unittest
from unittest.mock import patch

from friday.tools import shell


class LocalAppsAndShellTests(unittest.TestCase):
    def test_shell_defaults_to_user_home_cwd_and_allows_normal_programs(self):
        with patch.dict(os.environ, {}, clear=False):
            result = shell.propose_shell("python3 --version", reason="check python")

        self.assertEqual(result["status"], "pending_confirmation")
        self.assertEqual(result["cwd"], "/Users/dhruvsmac")
        self.assertEqual(result["program"], "python3")
        shell.cancel_shell(result["action_id"])

    def test_shell_still_blocks_privilege_escalation(self):
        with self.assertRaises(PermissionError):
            shell.propose_shell("sudo whoami", reason="bad")

    def test_local_note_requires_confirmation_before_osascript(self):
        from friday.tools import local_apps

        local_apps.clear_pending_local_app_actions()
        with patch("friday.tools.local_apps._osascript") as run:
            prepared = local_apps.prepare_local_note("Plan", "Write Friday tests")
            run.assert_not_called()
            result = local_apps.confirm_local_app_action(prepared["action_id"])

        run.assert_called_once()
        self.assertEqual(result["status"], "executed")
        self.assertIn("Notes", result["result"])

    def test_local_reminder_requires_confirmation_before_osascript(self):
        from friday.tools import local_apps

        local_apps.clear_pending_local_app_actions()
        with patch("friday.tools.local_apps._osascript") as run:
            prepared = local_apps.prepare_local_reminder("Call back", "After lunch")
            run.assert_not_called()
            result = local_apps.confirm_local_app_action(prepared["action_id"])

        run.assert_called_once()
        self.assertEqual(result["status"], "executed")
        self.assertIn("Reminders", result["result"])

    def test_local_app_tools_register_with_mcp(self):
        from friday.tools import local_apps

        class FakeMcp:
            def __init__(self):
                self.names = []

            def tool(self):
                def decorate(fn):
                    self.names.append(fn.__name__)
                    return fn

                return decorate

        mcp = FakeMcp()
        local_apps.register(mcp)

        self.assertEqual(
            mcp.names,
            [
                "prepare_local_note",
                "prepare_local_reminder",
                "confirm_local_app_action",
                "cancel_local_app_action",
            ],
        )


if __name__ == "__main__":
    unittest.main()
