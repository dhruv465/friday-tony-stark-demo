import os
import tempfile
import time
import unittest
from unittest.mock import MagicMock, patch

from friday.security import trust
from friday.tools import local_apps, shell, subagents


class TrustStateTests(unittest.TestCase):
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

    def test_disarmed_by_default(self):
        self.assertFalse(trust.is_armed())
        self.assertEqual(trust.status(), {"armed": False})

    def test_arm_then_disarm(self):
        status = trust.arm(duration_minutes=5)
        self.assertTrue(status["armed"])
        self.assertTrue(trust.is_armed())
        self.assertGreater(status["remaining_seconds"], 0)
        self.assertLessEqual(status["remaining_seconds"], 5 * 60)

        status = trust.disarm()
        self.assertFalse(status["armed"])
        self.assertFalse(trust.is_armed())

    def test_expiry_disarms(self):
        trust.arm(duration_minutes=5)
        state = trust._load_state()
        state["expires_ts"] = time.time() - 1
        trust._save_state(state)
        self.assertFalse(trust.is_armed())
        # Expiry rewrote the file — stays disarmed without re-checking time.
        self.assertFalse(trust._load_state().get("armed"))

    def test_duration_clamped_to_max(self):
        trust.arm(duration_minutes=100000)
        status = trust.status()
        self.assertLessEqual(status["remaining_seconds"], trust.MAX_MINUTES * 60)

    def test_default_minutes_env_override(self):
        with patch.dict(os.environ, {"FRIDAY_TRUST_DEFAULT_MINUTES": "7"}):
            self.assertEqual(trust.default_minutes(), 7)
        with patch.dict(os.environ, {"FRIDAY_TRUST_DEFAULT_MINUTES": "junk"}):
            self.assertEqual(trust.default_minutes(), trust.DEFAULT_MINUTES)

    def test_short_circuit_only_when_armed(self):
        confirm = MagicMock(return_value={"status": "executed"})
        self.assertIsNone(trust.trusted_short_circuit(confirm, "abc"))
        confirm.assert_not_called()

        trust.arm(duration_minutes=5)
        result = trust.trusted_short_circuit(confirm, "abc")
        confirm.assert_called_once_with("abc")
        self.assertEqual(result, {"status": "executed", "trust_mode": True})


class TrustBrokerShortCircuitTests(unittest.TestCase):
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
        shell.PENDING_SHELL_ACTIONS.clear()
        local_apps.clear_pending_local_app_actions()
        subagents.clear_pending_subagent_actions()

    def tearDown(self):
        shell.PENDING_SHELL_ACTIONS.clear()
        local_apps.clear_pending_local_app_actions()
        subagents.clear_pending_subagent_actions()
        self._env.stop()
        self._tmp.cleanup()

    def test_shell_pending_without_trust(self):
        result = shell.propose_shell("echo hello")
        self.assertEqual(result["status"], "pending_confirmation")
        self.assertEqual(len(shell.PENDING_SHELL_ACTIONS), 1)

    def test_shell_executes_immediately_with_trust(self):
        trust.arm(duration_minutes=5)
        result = shell.propose_shell("echo trusted")
        self.assertEqual(result["status"], "executed")
        self.assertTrue(result["trust_mode"])
        self.assertIn("trusted", result["stdout"])
        self.assertEqual(len(shell.PENDING_SHELL_ACTIONS), 0)

    def test_shell_denylist_still_blocks_under_trust(self):
        trust.arm(duration_minutes=5)
        with self.assertRaises(PermissionError):
            shell.propose_shell("sudo whoami")
        with self.assertRaises(ValueError):
            shell.propose_shell("echo a && echo b")

    def test_local_app_executes_immediately_with_trust(self):
        trust.arm(duration_minutes=5)
        with patch.object(local_apps, "_osascript") as osa:
            result = local_apps.prepare_local_note("Groceries", "milk")
        osa.assert_called_once()
        self.assertEqual(result["status"], "executed")
        self.assertTrue(result["trust_mode"])

    def test_local_app_pending_without_trust(self):
        with patch.object(local_apps, "_osascript") as osa:
            result = local_apps.prepare_local_note("Groceries", "milk")
        osa.assert_not_called()
        self.assertEqual(result["status"], "pending_confirmation")

    def test_subagent_deploys_immediately_with_trust(self):
        trust.arm(duration_minutes=5)
        fake = MagicMock()
        fake.is_active.return_value = False
        fake.start_agent.return_value = {"status": "agent_deployed", "name": "Scout", "slug": "scout"}
        with patch.object(subagents, "_runtime", return_value=fake):
            result = subagents.propose_subagent("compare A and B", name="Scout")
        fake.start_agent.assert_called_once()
        self.assertEqual(result["status"], "agent_deployed")
        self.assertTrue(result["trust_mode"])

    def test_tier2_messaging_unaffected_by_trust(self):
        # Hard floor: the messaging broker must not consult trust mode.
        import inspect

        from friday.tools import messaging

        source = inspect.getsource(messaging)
        self.assertNotIn("trusted_short_circuit", source)
        self.assertNotIn("from friday.security", source)

    def test_tier2_security_remediation_unaffected_by_trust(self):
        trust.arm(duration_minutes=5)
        from friday.tools import security

        result = security.propose_remediation("kill_process", str(10**6))
        self.assertEqual(result["status"], "pending_confirmation")
        security.clear_pending_security_actions()


if __name__ == "__main__":
    unittest.main()
