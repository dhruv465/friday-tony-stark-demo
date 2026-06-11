import asyncio
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from friday.learning import engine, store


def _plan(queries, coverage="developing", done=False):
    return {"queries": queries, "coverage": coverage, "done": done, "rationale": ""}


def _distilled(coverage="developing"):
    return {
        "index_md": "# Topic\n\n## Summary\nLearned things.",
        "open_questions_md": "- [ ] what about edge cases?",
        "coverage": coverage,
        "subtopic_notes": [],
    }


class LearningEngineTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._env = patch.dict(
            os.environ,
            {
                "FRIDAY_KNOWLEDGE_DIR": self._tmp.name,
                "FRIDAY_DESKTOP_EVENT_LOG": str(Path(self._tmp.name) / "events.jsonl"),
                "FRIDAY_LEARNER_FETCH_DELAY_S": "0",
                "FRIDAY_LEARNER_BACKOFF_BASE_S": "0",
                "FRIDAY_LEARNER_MAX_PAGES_PER_ROUND": "2",
                "FRIDAY_LEARNER_MAX_ROUNDS": "5",
            },
            clear=False,
        )
        self._env.start()

    def tearDown(self):
        self._env.stop()
        self._tmp.cleanup()

    def test_run_round_dedupes_seen_urls_and_respects_page_budget(self):
        slug = "rust-async"
        job = store.new_job("rust async", slug)
        job["sources_seen"] = ["https://seen.example/a"]
        store.save_job(slug, job)

        search_results = [
            ("Seen", "https://seen.example/a", ""),
            ("New B", "https://new.example/b", ""),
            ("New C", "https://new.example/c", ""),
            ("New D", "https://new.example/d", ""),
        ]
        fetch = AsyncMock(return_value="plenty of text about rust async runtimes")
        with patch(
            "friday.tools.web.ddg_search_raw", AsyncMock(return_value=search_results)
        ), patch(
            "friday.learning.extract.allowed_by_robots", AsyncMock(return_value=True)
        ), patch(
            "friday.learning.extract.fetch_page_text", fetch
        ), patch(
            "friday.learning.llm.generate_queries",
            AsyncMock(return_value=_plan(["rust async overview"])),
        ), patch(
            "friday.learning.llm.distill", AsyncMock(return_value=_distilled())
        ):
            ok = asyncio.run(engine._run_round(object(), job, {}))

        self.assertTrue(ok)
        # Budget is 2 pages, and the already-seen URL is never fetched.
        self.assertEqual(fetch.await_count, 2)
        fetched_urls = [call.args[1] for call in fetch.await_args_list]
        self.assertNotIn("https://seen.example/a", fetched_urls)

        saved = store.load_job(slug)
        self.assertEqual(saved["rounds_completed"], 1)
        self.assertIn("https://new.example/b", saved["sources_seen"])
        self.assertIn("rust async overview", saved["queries_run"])
        self.assertIn("Learned things.", store.read_doc(slug, "index.md"))
        self.assertIn("edge cases", store.read_doc(slug, "open-questions.md"))
        self.assertIn("https://new.example/b", store.read_doc(slug, "sources.md"))

    def test_consecutive_failures_mark_stalled(self):
        slug = "rust-async"
        with patch.object(engine, "_run_round", AsyncMock(return_value=False)):
            asyncio.run(engine._run_job("rust async", slug))

        job = store.load_job(slug)
        self.assertEqual(job["status"], "stalled")
        self.assertIsNotNone(job["stalled_until"])
        self.assertEqual(job["consecutive_failures"], engine.STALL_FAILURES)
        self.assertFalse((store.topic_dir(slug) / store.LOCK_NAME).exists())

    def test_pause_status_checked_between_rounds(self):
        slug = "rust-async"

        async def _round_then_pause(client, job, robots_cache):
            job["rounds_completed"] = job.get("rounds_completed", 0) + 1
            job["rounds_this_session"] = job.get("rounds_this_session", 0) + 1
            engine._save_preserving_control(slug, job)
            # The boss pauses while the round runs (other process / tool call).
            disk = store.load_job(slug)
            disk["status"] = "paused"
            store.save_job(slug, disk)
            return True

        with patch.object(engine, "_run_round", _round_then_pause):
            asyncio.run(engine._run_job("rust async", slug))

        job = store.load_job(slug)
        self.assertEqual(job["status"], "paused")
        self.assertEqual(job["rounds_this_session"], 1)

    def test_resume_in_progress_picks_up_orphan_running_job(self):
        slug = "rust-async"
        job = store.new_job("rust async", slug)
        job["status"] = "running"  # crashed process left it running, no lock
        store.save_job(slug, job)

        runtime = engine.LearningRuntime()
        runtime.start_job = MagicMock(return_value={"status": "learning_resumed"})
        resumed = runtime.resume_in_progress()

        self.assertEqual(resumed, [slug])
        runtime.start_job.assert_called_once_with("rust async", resumed=True)

    def test_resume_skips_paused_and_future_stalled_jobs(self):
        paused = store.new_job("paused topic", "paused-topic")
        paused["status"] = "paused"
        store.save_job("paused-topic", paused)

        stalled = store.new_job("stalled topic", "stalled-topic")
        stalled["status"] = "stalled"
        stalled["stalled_until"] = "2999-01-01T00:00:00"
        store.save_job("stalled-topic", stalled)

        runtime = engine.LearningRuntime()
        runtime.start_job = MagicMock()
        self.assertEqual(runtime.resume_in_progress(), [])
        runtime.start_job.assert_not_called()


if __name__ == "__main__":
    unittest.main()
