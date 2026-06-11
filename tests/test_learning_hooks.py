import asyncio
import os
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from friday.agents import runtime


class LearningHooksTests(unittest.TestCase):
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

    def _finish_job(self, slug: str, status: str, report: str = "") -> None:
        record = runtime.new_record(slug.title(), slug, f"research {slug}", "")
        runtime.save_record(slug, record)
        if report:
            runtime.save_report(slug, report)

        async def fake_run(s):
            r = runtime.load_record(s)
            r["status"] = status
            if status == "failed":
                r["error"] = "step limit (3) reached without a report"
            runtime.save_record(s, r)

        with patch.object(runtime, "_run_agent", fake_run), patch.object(
            runtime, "_mac_notification", MagicMock()
        ):
            asyncio.run(runtime._run_agent_job(slug))

    def test_outcome_note_written_on_complete(self):
        self._finish_job("rust-vs-go", "complete", report="# Verdict\nRust for this.")
        outcome = (runtime.agent_dir("rust-vs-go") / runtime.OUTCOME_MD).read_text()
        self.assertIn("status: complete", outcome)
        self.assertIn("research rust-vs-go", outcome)
        self.assertIn("Rust for this.", outcome)

    def test_outcome_note_written_on_failure_with_error(self):
        self._finish_job("doomed", "failed")
        outcome = (runtime.agent_dir("doomed") / runtime.OUTCOME_MD).read_text()
        self.assertIn("status: failed", outcome)
        self.assertIn("step limit", outcome)

    def test_no_outcome_note_when_stopped(self):
        self._finish_job("halted", "stopped")
        self.assertFalse((runtime.agent_dir("halted") / runtime.OUTCOME_MD).is_file())

    def test_lessons_recalled_from_past_outcomes(self):
        self._finish_job("rust-vs-go", "complete", report="Rust won on safety.")
        lessons = runtime._lessons_for("compare rust and go performance", "new-job")
        self.assertIn("Lessons from your past jobs", lessons)
        self.assertIn("rust", lessons.lower())

    def test_own_outcome_excluded_from_lessons(self):
        self._finish_job("rust-vs-go", "complete", report="Rust won.")
        lessons = runtime._lessons_for("research rust-vs-go", "rust-vs-go")
        self.assertEqual(lessons, "")

    def test_no_lessons_from_plain_reports(self):
        # report.md alone (no outcome note) must not leak into lessons.
        slug = "raw"
        runtime.save_record(slug, runtime.new_record("Raw", slug, "task", ""))
        runtime.save_report(slug, "quantum widgets are trending")
        lessons = runtime._lessons_for("quantum widgets", "other")
        self.assertEqual(lessons, "")

    def test_lessons_injected_into_prompt(self):
        record = runtime.new_record("Scout", "scout", "task", "")
        prompt = runtime._worker_prompt(record, "Lessons from your past jobs: X")
        self.assertIn("Lessons from your past jobs", prompt)
        self.assertNotIn("Lessons", runtime._worker_prompt(record))


if __name__ == "__main__":
    unittest.main()
