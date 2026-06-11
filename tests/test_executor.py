import asyncio
import json
import os
import tempfile
import types
import unittest
from unittest.mock import AsyncMock, patch

from friday.agents import runtime
from friday.security import trust


def _tool_call(call_id: str, name: str, arguments: dict):
    return types.SimpleNamespace(
        id=call_id,
        function=types.SimpleNamespace(name=name, arguments=json.dumps(arguments)),
    )


def _llm_response(content=None, tool_calls=None):
    message = types.SimpleNamespace(content=content, tool_calls=tool_calls or [])
    return types.SimpleNamespace(choices=[types.SimpleNamespace(message=message)])


class ExecutorEnvMixin(unittest.TestCase):
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


class RegistryTests(ExecutorEnvMixin):
    def test_untrusted_job_gets_tier0_only(self):
        record = runtime.new_record("Scout", "scout", "task", "")
        registry = runtime._registry_for(record)
        self.assertEqual(
            set(registry), {"web_search", "fetch_page", "recall_knowledge", "read_file"}
        )

    def test_trusted_job_gets_hands(self):
        record = runtime.new_record("Scout", "scout", "task", "", trusted=True)
        registry = runtime._registry_for(record)
        self.assertIn("run_shell", registry)
        self.assertIn("write_file", registry)
        self.assertIn("create_note", registry)
        self.assertIn("send_message", registry)

    def test_write_report_always_in_payload(self):
        record = runtime.new_record("Scout", "scout", "task", "")
        payload = runtime._tools_payload(runtime._registry_for(record))
        names = [t["function"]["name"] for t in payload]
        self.assertIn("write_report", names)

    def test_trusted_prompt_mentions_hands(self):
        trusted = runtime.new_record("Scout", "scout", "task", "", trusted=True)
        untrusted = runtime.new_record("Scout", "scout", "task", "")
        self.assertIn("run_shell", runtime._worker_prompt(trusted))
        self.assertNotIn("run_shell", runtime._worker_prompt(untrusted))

    def test_start_agent_samples_trust_at_deploy(self):
        rt = runtime.SubagentRuntime()
        with patch.object(runtime, "_run_agent", AsyncMock()):
            trust.arm(duration_minutes=5)
            armed = rt.start_agent("Armed", "task one")
            trust.disarm()
            disarmed = rt.start_agent("Disarmed", "task two")
        self.assertTrue(armed["trusted"])
        self.assertFalse(disarmed["trusted"])

    def test_step_budget_by_job_type(self):
        with patch.dict(os.environ, {"FRIDAY_SUBAGENT_MAX_STEPS": ""}):
            self.assertEqual(runtime._max_steps("quick"), 15)
            self.assertEqual(runtime._max_steps("deep"), 40)
        with patch.dict(os.environ, {"FRIDAY_SUBAGENT_MAX_STEPS": "3"}):
            self.assertEqual(runtime._max_steps("deep"), 3)


class FileScopingTests(ExecutorEnvMixin):
    def test_relative_path_lands_in_work_dir(self):
        path = runtime._resolve_in_roots("scout", "notes.md")
        self.assertTrue(str(path).startswith(str(runtime.work_dir("scout"))))

    def test_escape_rejected(self):
        with self.assertRaises(PermissionError):
            runtime._resolve_in_roots("scout", "../../../etc/passwd")
        with self.assertRaises(PermissionError):
            runtime._resolve_in_roots("scout", "/etc/passwd")

    def test_whitelisted_root_allowed(self):
        with tempfile.TemporaryDirectory() as extra:
            with patch.dict(os.environ, {"FRIDAY_EXECUTOR_FILE_ROOTS": extra}):
                path = runtime._resolve_in_roots("scout", os.path.join(extra, "x.txt"))
                self.assertTrue(str(path).startswith(os.path.realpath(extra)))

    def test_write_then_read_roundtrip(self):
        async def go():
            wrote = await runtime._tool_write_file(
                None, "scout", {"path": "out.md", "content": "# hello"}
            )
            read = await runtime._tool_read_file(None, "scout", {"path": "out.md"})
            return wrote, read

        wrote, read = asyncio.run(go())
        self.assertIn("Wrote", wrote)
        self.assertEqual(read, "# hello")


class TrustedToolTests(ExecutorEnvMixin):
    def test_run_shell_executes_under_trust(self):
        trust.arm(duration_minutes=5)
        result = asyncio.run(
            runtime._tool_run_shell(None, "scout", {"command": "echo executor", "reason": "test"})
        )
        data = json.loads(result)
        self.assertEqual(data["status"], "executed")
        self.assertIn("executor", data["stdout"])

    def test_run_shell_raises_trust_lapsed_when_disarmed(self):
        trust.disarm()
        with self.assertRaises(runtime.TrustLapsed):
            asyncio.run(
                runtime._tool_run_shell(None, "scout", {"command": "echo nope", "reason": ""})
            )

    def test_send_message_only_stages(self):
        staged = {"status": "pending_confirmation", "summary": "to Pepper", "channel": "imessage"}
        with patch("friday.tools.messaging.prepare_outbound_message", return_value=staged):
            result = asyncio.run(
                runtime._tool_send_message(
                    None,
                    "scout",
                    {"channel": "imessage", "recipient": "Pepper", "message": "hi"},
                )
            )
        self.assertIn("NOT send", result)


class PauseOnTrustExpiryTests(ExecutorEnvMixin):
    def test_trusted_job_pauses_then_resumes(self):
        slug = "scout"
        runtime.save_record(
            slug, runtime.new_record("Scout", slug, "task", "", trusted=True)
        )
        # Trust starts disarmed → first loop iteration must pause. Re-arm
        # from inside the sleep so the job resumes and finishes.
        responses = [_llm_response(content="done.")]

        original_sleep = asyncio.sleep
        rearmed = {"done": False}

        async def fake_sleep(seconds):
            if not rearmed["done"]:
                trust.arm(duration_minutes=5)
                rearmed["done"] = True
            await original_sleep(0)

        statuses = []
        original_save = runtime.save_record

        def spy_save(s, record):
            statuses.append(record.get("status"))
            original_save(s, record)

        with patch.object(runtime, "_chat", AsyncMock(side_effect=responses)), patch(
            "friday.agents.runtime.asyncio.sleep", fake_sleep
        ), patch.object(runtime, "save_record", spy_save):
            asyncio.run(runtime._run_agent(slug))

        self.assertIn("paused_awaiting_trust", statuses)
        self.assertEqual(runtime.load_record(slug)["status"], "complete")

    def test_paused_job_stoppable(self):
        slug = "scout"
        runtime.save_record(
            slug, runtime.new_record("Scout", slug, "task", "", trusted=True)
        )
        original_sleep = asyncio.sleep

        async def stop_during_sleep(seconds):
            record = runtime.load_record(slug)
            record["status"] = "stopped"
            runtime.save_record(slug, record)
            await original_sleep(0)

        with patch.object(runtime, "_chat", AsyncMock()) as chat, patch(
            "friday.agents.runtime.asyncio.sleep", stop_during_sleep
        ):
            asyncio.run(runtime._run_agent(slug))

        chat.assert_not_awaited()
        self.assertEqual(runtime.load_record(slug)["status"], "stopped")

    def test_pause_timeout_fails_job(self):
        slug = "scout"
        runtime.save_record(
            slug, runtime.new_record("Scout", slug, "task", "", trusted=True)
        )
        original_sleep = asyncio.sleep

        async def quick_sleep(seconds):
            await original_sleep(0)

        with patch.object(runtime, "_chat", AsyncMock()), patch(
            "friday.agents.runtime.asyncio.sleep", quick_sleep
        ), patch.dict(os.environ, {"FRIDAY_EXECUTOR_PAUSE_MAX_S": "0"}):
            asyncio.run(runtime._run_agent(slug))

        record = runtime.load_record(slug)
        self.assertEqual(record["status"], "failed")
        self.assertIn("trust", record["error"])


class StepBudgetTests(ExecutorEnvMixin):
    def test_final_step_forces_write_report(self):
        slug = "scout"
        runtime.save_record(slug, runtime.new_record("Scout", slug, "task", ""))
        responses = [
            _llm_response(tool_calls=[_tool_call("c1", "web_search", {"query": "x"})]),
            _llm_response(
                tool_calls=[_tool_call("c2", "write_report", {"report_markdown": "# done"})]
            ),
        ]
        chat = AsyncMock(side_effect=responses)
        with patch.object(runtime, "_chat", chat), patch.object(
            runtime, "_execute_tool", AsyncMock(return_value="ok")
        ), patch.dict(os.environ, {"FRIDAY_SUBAGENT_MAX_STEPS": "2"}):
            asyncio.run(runtime._run_agent(slug))

        self.assertEqual(runtime.load_record(slug)["status"], "complete")
        self.assertEqual(runtime.read_report(slug), "# done")
        # The final step must offer only write_report and force it.
        final_args, final_kwargs = chat.call_args_list[-1]
        self.assertEqual(final_args[1], [runtime.WRITE_REPORT_SCHEMA])
        self.assertEqual(final_kwargs.get("force_tool"), "write_report")
        contents = [m.get("content") for m in final_args[0]]
        self.assertIn(runtime.BUDGET_FINAL_MSG, contents)

    def test_budget_warning_injected_before_final_step(self):
        slug = "scout"
        runtime.save_record(slug, runtime.new_record("Scout", slug, "task", ""))
        responses = [
            _llm_response(tool_calls=[_tool_call("c1", "web_search", {"query": "x"})]),
            _llm_response(tool_calls=[_tool_call("c2", "web_search", {"query": "y"})]),
            _llm_response(content="plain answer"),
        ]
        chat = AsyncMock(side_effect=responses)
        with patch.object(runtime, "_chat", chat), patch.object(
            runtime, "_execute_tool", AsyncMock(return_value="ok")
        ), patch.dict(os.environ, {"FRIDAY_SUBAGENT_MAX_STEPS": "3"}):
            asyncio.run(runtime._run_agent(slug))

        warn_args, _ = chat.call_args_list[1]
        contents = [m.get("content") for m in warn_args[0]]
        self.assertIn(runtime.BUDGET_WARNING_MSG, contents)

    def test_salvage_partial_report_when_no_report_written(self):
        slug = "scout"
        runtime.save_record(slug, runtime.new_record("Scout", slug, "task", ""))
        # Even the forced final step misbehaves and calls a research tool —
        # the transcript must still be salvaged into a partial report.
        responses = [
            _llm_response(tool_calls=[_tool_call("c1", "web_search", {"query": "x"})]),
            _llm_response(tool_calls=[_tool_call("c2", "web_search", {"query": "y"})]),
        ]
        with patch.object(runtime, "_chat", AsyncMock(side_effect=responses)), patch.object(
            runtime, "_execute_tool", AsyncMock(return_value="finding A")
        ), patch.dict(os.environ, {"FRIDAY_SUBAGENT_MAX_STEPS": "2"}):
            asyncio.run(runtime._run_agent(slug))

        record = runtime.load_record(slug)
        self.assertEqual(record["status"], "failed")
        self.assertIn("partial findings saved", record["error"])
        report = runtime.read_report(slug)
        self.assertIn("Partial findings", report)
        self.assertIn("finding A", report)

    def test_partial_report_empty_when_no_findings(self):
        self.assertEqual(runtime._partial_report([{"role": "user", "content": "task"}]), "")


if __name__ == "__main__":
    unittest.main()
