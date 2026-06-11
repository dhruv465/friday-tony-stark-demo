import os
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from friday.learning import store
from friday.tools import learning


class LearningToolsTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._env = patch.dict(
            os.environ, {"FRIDAY_KNOWLEDGE_DIR": self._tmp.name}, clear=False
        )
        self._env.start()
        learning.clear_pending_learning_actions()

    def tearDown(self):
        learning.clear_pending_learning_actions()
        self._env.stop()
        self._tmp.cleanup()

    def _fake_runtime(self):
        runtime = MagicMock()
        runtime.is_active.return_value = False
        runtime.start_job.return_value = {
            "status": "learning_started",
            "topic": "rust async",
            "slug": "rust-async",
        }
        return runtime

    def test_confirm_required_before_job_starts(self):
        runtime = self._fake_runtime()
        with patch("friday.tools.learning._runtime", return_value=runtime):
            pending = learning.propose_learning("rust async", reason="boss asked")
            runtime.start_job.assert_not_called()
            self.assertEqual(pending["status"], "pending_confirmation")

            result = learning.confirm_learning()

        runtime.start_job.assert_called_once_with("rust async")
        self.assertEqual(result["status"], "learning_started")

    def test_cancel_drops_pending(self):
        with patch("friday.tools.learning._runtime", return_value=self._fake_runtime()):
            pending = learning.propose_learning("rust async")
            result = learning.cancel_learning(pending["action_id"])
            self.assertEqual(result["status"], "cancelled")
            with self.assertRaises(KeyError):
                learning.confirm_learning()

    def test_propose_known_complete_topic_short_circuits(self):
        job = store.new_job("rust async", "rust-async")
        job["status"] = "complete"
        job["coverage"] = "good"
        store.save_job("rust-async", job)

        runtime = self._fake_runtime()
        with patch("friday.tools.learning._runtime", return_value=runtime):
            result = learning.propose_learning("rust async")

        self.assertEqual(result["status"], "already_known")
        runtime.start_job.assert_not_called()

    def test_recall_knowledge_returns_index_for_best_hit(self):
        store.write_doc(
            "zeta-widgets",
            "index.md",
            "# Zeta Widgets\n\nZeta widgets are flux-coupled gizmos.",
        )
        text = learning.recall_knowledge("zeta widgets")
        self.assertIn("LEARNED KNOWLEDGE", text)
        self.assertIn("flux-coupled", text)
        self.assertEqual(
            learning.recall_knowledge("completely unrelated thing xyzzy"),
            "No learned knowledge on that yet.",
        )

    def test_learning_tools_register_with_mcp(self):
        class FakeMcp:
            def __init__(self):
                self.names = []

            def tool(self):
                def decorate(fn):
                    self.names.append(fn.__name__)
                    return fn

                return decorate

        mcp = FakeMcp()
        learning.register(mcp)

        self.assertEqual(
            mcp.names,
            [
                "propose_learning",
                "confirm_learning",
                "cancel_learning",
                "learning_status",
                "pause_learning",
                "stop_learning",
                "continue_learning",
                "list_known_topics",
                "recall_knowledge",
            ],
        )


if __name__ == "__main__":
    unittest.main()
