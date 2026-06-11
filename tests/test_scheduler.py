import asyncio
import json
import os
import tempfile
import time
import types
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from friday.agents import runtime
from friday.tools import subagents


def _llm_response(content=None):
    message = types.SimpleNamespace(content=content, tool_calls=[])
    return types.SimpleNamespace(choices=[types.SimpleNamespace(message=message)])


class SchedulerEnvMixin(unittest.TestCase):
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


class ScheduleValidationTests(SchedulerEnvMixin):
    def test_none_passthrough(self):
        self.assertIsNone(runtime.validate_schedule(None))
        self.assertIsNone(runtime.validate_schedule({}))

    def test_at_parses_iso(self):
        schedule = runtime.validate_schedule({"at": "2026-06-11T18:00"})
        self.assertIn("at_ts", schedule)

    def test_bad_at_rejected(self):
        with self.assertRaises(ValueError):
            runtime.validate_schedule({"at": "six pm"})

    def test_every_minutes_floor(self):
        with self.assertRaises(ValueError):
            runtime.validate_schedule({"every_minutes": 0})
        self.assertEqual(
            runtime.validate_schedule({"every_minutes": 60}), {"every_minutes": 60}
        )

    def test_propose_rejects_both_schedules(self):
        with self.assertRaises(ValueError):
            subagents.propose_subagent(
                "watch X", schedule_at="2026-06-11T18:00", every_minutes=5
            )


class TickTests(SchedulerEnvMixin):
    def _scheduled_record(self, slug: str, next_run_ts: float) -> dict:
        record = runtime.new_record("Watcher", slug, "watch the thing", "")
        record["status"] = "scheduled"
        record["next_run_ts"] = next_run_ts
        runtime.save_record(slug, record)
        return record

    def test_due_job_fires_once(self):
        rt = runtime.SubagentRuntime()
        self._scheduled_record("watcher", time.time() - 5)
        launched = []

        async def fake_guarded(slug):
            launched.append(slug)

        async def go():
            with patch.object(rt, "_guarded", fake_guarded):
                rt._tick(time.time())
                # Second tick: status now "running" → must not re-fire.
                rt._tick(time.time())
                await asyncio.sleep(0)

        asyncio.run(go())
        self.assertEqual(launched, ["watcher"])
        self.assertEqual(runtime.load_record("watcher")["status"], "running")

    def test_future_job_not_fired(self):
        rt = runtime.SubagentRuntime()
        self._scheduled_record("watcher", time.time() + 3600)

        async def go():
            with patch.object(rt, "_guarded", AsyncMock()) as guarded:
                rt._tick(time.time())
                await asyncio.sleep(0)
                guarded.assert_not_called()

        asyncio.run(go())
        self.assertEqual(runtime.load_record("watcher")["status"], "scheduled")

    def test_stopped_schedule_not_fired(self):
        rt = runtime.SubagentRuntime()
        record = self._scheduled_record("watcher", time.time() - 5)
        record["status"] = "stopped"
        runtime.save_record("watcher", record)

        async def go():
            rt._tick(time.time())
            await asyncio.sleep(0)

        asyncio.run(go())
        self.assertEqual(runtime.load_record("watcher")["status"], "stopped")

    def test_request_stop_cancels_scheduled(self):
        rt = runtime.SubagentRuntime()
        self._scheduled_record("watcher", time.time() + 3600)
        result = rt.request_stop("watcher")
        self.assertEqual(result["status"], "stopped")


class StartAgentSchedulingTests(SchedulerEnvMixin):
    def test_delayed_oneshot_parks_as_scheduled(self):
        rt = runtime.SubagentRuntime()
        with patch.object(runtime, "_run_agent", AsyncMock()):
            result = rt.start_agent(
                "Later", "do it later", schedule={"at": "2099-01-01T09:00"}
            )
        self.assertEqual(result["status"], "agent_scheduled")
        record = runtime.load_record("later")
        self.assertEqual(record["status"], "scheduled")
        self.assertIsNotNone(record["next_run_ts"])

    def test_monitor_fires_immediately(self):
        rt = runtime.SubagentRuntime()
        with patch.object(runtime, "_run_agent_job", AsyncMock()):
            result = rt.start_agent("Watch", "watch X", schedule={"every_minutes": 30})
        self.assertEqual(result["status"], "agent_deployed")


class MonitorRunTests(SchedulerEnvMixin):
    def test_monitor_reschedules_and_notifies_on_change(self):
        slug = "watch"
        record = runtime.new_record(
            "Watch", slug, "watch X", "", schedule={"every_minutes": 30}
        )
        runtime.save_record(slug, record)
        runtime.save_report(slug, "old state")

        async def fake_run(s):
            r = runtime.load_record(s)
            r["status"] = "complete"
            runtime.save_record(s, r)
            runtime.save_report(s, "new state — price dropped")

        with patch.object(runtime, "_run_agent", fake_run), patch.object(
            runtime, "_monitor_changed", AsyncMock(return_value=True)
        ), patch.object(runtime, "_mac_notification", MagicMock()) as notif:
            asyncio.run(runtime._run_agent_job(slug))

        record = runtime.load_record(slug)
        self.assertEqual(record["status"], "scheduled")
        self.assertGreater(record["next_run_ts"], time.time())
        self.assertEqual(record["runs_completed"], 1)
        notif.assert_called_once()
        # Previous report archived for the next diff.
        prev = (runtime.agent_dir(slug) / runtime.PREV_REPORT_MD).read_text()
        self.assertEqual(prev, "old state")
        # Pending notify flag set for the voice agent.
        news = runtime.pop_pending_notifies()
        self.assertEqual(len(news), 1)
        self.assertEqual(news[0]["slug"], slug)
        # Flag cleared after pop.
        self.assertEqual(runtime.pop_pending_notifies(), [])

    def test_monitor_silent_when_unchanged(self):
        slug = "watch"
        record = runtime.new_record(
            "Watch", slug, "watch X", "", schedule={"every_minutes": 30}
        )
        runtime.save_record(slug, record)
        runtime.save_report(slug, "same state")

        async def fake_run(s):
            r = runtime.load_record(s)
            r["status"] = "complete"
            runtime.save_record(s, r)
            runtime.save_report(s, "same state")

        with patch.object(runtime, "_run_agent", fake_run), patch.object(
            runtime, "_mac_notification", MagicMock()
        ) as notif:
            asyncio.run(runtime._run_agent_job(slug))

        notif.assert_not_called()
        self.assertEqual(runtime.pop_pending_notifies(), [])
        self.assertEqual(runtime.load_record(slug)["status"], "scheduled")

    def test_first_monitor_report_always_news(self):
        changed = asyncio.run(runtime._monitor_changed("", "anything"))
        self.assertTrue(changed)

    def test_identical_reports_unchanged_without_llm(self):
        changed = asyncio.run(runtime._monitor_changed("same", "same"))
        self.assertFalse(changed)

    def test_oneshot_completion_sets_notify(self):
        slug = "once"
        runtime.save_record(slug, runtime.new_record("Once", slug, "task", ""))

        async def fake_run(s):
            r = runtime.load_record(s)
            r["status"] = "complete"
            runtime.save_record(s, r)
            runtime.save_report(s, "done")

        with patch.object(runtime, "_run_agent", fake_run), patch.object(
            runtime, "_mac_notification", MagicMock()
        ):
            asyncio.run(runtime._run_agent_job(slug))

        news = runtime.pop_pending_notifies()
        self.assertEqual(len(news), 1)
        self.assertEqual(runtime.load_record(slug)["status"], "complete")


if __name__ == "__main__":
    unittest.main()
