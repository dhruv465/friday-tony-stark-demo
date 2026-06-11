import asyncio
import json
import os
import tempfile
import types
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from friday.agents import runtime
from friday.tools import subagents


def _tool_call(call_id: str, name: str, arguments: dict):
    return types.SimpleNamespace(
        id=call_id,
        function=types.SimpleNamespace(name=name, arguments=json.dumps(arguments)),
    )


def _llm_response(content=None, tool_calls=None):
    message = types.SimpleNamespace(content=content, tool_calls=tool_calls or [])
    return types.SimpleNamespace(choices=[types.SimpleNamespace(message=message)])


class SubagentRuntimeTests(unittest.TestCase):
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

    def test_worker_runs_tools_then_writes_report(self):
        slug = "scout"
        runtime.save_record(slug, runtime.new_record("Scout", slug, "compare A and B", ""))

        responses = [
            _llm_response(tool_calls=[_tool_call("c1", "web_search", {"query": "A vs B"})]),
            _llm_response(
                tool_calls=[_tool_call("c2", "write_report", {"report_markdown": "# Verdict\nA wins."})]
            ),
        ]
        search = AsyncMock(return_value=[("A vs B", "https://x.example/a", "snippet")])
        with patch.object(runtime, "_chat", AsyncMock(side_effect=responses)), patch(
            "friday.tools.web.ddg_search_raw", search
        ):
            asyncio.run(runtime._run_agent(slug))

        record = runtime.load_record(slug)
        self.assertEqual(record["status"], "complete")
        self.assertEqual(record["steps_taken"], 2)
        search.assert_awaited_once()
        self.assertIn("A wins.", runtime.read_report(slug))

    def test_plain_text_answer_becomes_report(self):
        slug = "scout"
        runtime.save_record(slug, runtime.new_record("Scout", slug, "say hi", ""))
        with patch.object(runtime, "_chat", AsyncMock(return_value=_llm_response(content="Hi boss."))):
            asyncio.run(runtime._run_agent(slug))

        self.assertEqual(runtime.load_record(slug)["status"], "complete")
        self.assertEqual(runtime.read_report(slug), "Hi boss.")

    def test_stop_flag_checked_between_steps(self):
        slug = "scout"
        runtime.save_record(slug, runtime.new_record("Scout", slug, "endless", ""))

        async def _stop_then_loop(messages, tools):
            record = runtime.load_record(slug)
            record["status"] = "stopped"
            runtime.save_record(slug, record)
            return _llm_response(tool_calls=[_tool_call("c", "web_search", {"query": "x"})])

        with patch.object(runtime, "_chat", AsyncMock(side_effect=_stop_then_loop)), patch(
            "friday.tools.web.ddg_search_raw", AsyncMock(return_value=[])
        ):
            asyncio.run(runtime._run_agent(slug))

        self.assertEqual(runtime.load_record(slug)["status"], "stopped")

    def test_step_limit_marks_failed(self):
        slug = "scout"
        runtime.save_record(slug, runtime.new_record("Scout", slug, "never ends", ""))
        looping = _llm_response(tool_calls=[_tool_call("c", "web_search", {"query": "x"})])
        with patch.dict(os.environ, {"FRIDAY_SUBAGENT_MAX_STEPS": "3"}), patch.object(
            runtime, "_chat", AsyncMock(return_value=looping)
        ), patch("friday.tools.web.ddg_search_raw", AsyncMock(return_value=[])):
            asyncio.run(runtime._run_agent(slug))

        record = runtime.load_record(slug)
        self.assertEqual(record["status"], "failed")
        self.assertIn("step limit", record["error"])


class SubagentToolsTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._env = patch.dict(
            os.environ, {"FRIDAY_KNOWLEDGE_DIR": self._tmp.name}, clear=False
        )
        self._env.start()
        subagents.clear_pending_subagent_actions()

    def tearDown(self):
        subagents.clear_pending_subagent_actions()
        self._env.stop()
        self._tmp.cleanup()

    def test_confirm_required_before_deploy(self):
        fake = MagicMock()
        fake.is_active.return_value = False
        fake.start_agent.return_value = {"status": "agent_deployed", "slug": "scout"}
        with patch("friday.tools.subagents._runtime", return_value=fake):
            pending = subagents.propose_subagent("compare A and B", name="Scout", reason="background dig")
            fake.start_agent.assert_not_called()
            self.assertEqual(pending["status"], "pending_confirmation")
            result = subagents.confirm_subagent()

        fake.start_agent.assert_called_once_with(
            "Scout", "compare A and B", "background dig", job_type="quick", schedule=None
        )
        self.assertEqual(result["status"], "agent_deployed")

    def test_cancel_drops_pending(self):
        with patch("friday.tools.subagents._runtime", return_value=MagicMock(is_active=lambda s: False)):
            pending = subagents.propose_subagent("task one")
        result = subagents.cancel_subagent(pending["action_id"])
        self.assertEqual(result["status"], "cancelled")
        with self.assertRaises(KeyError):
            subagents.confirm_subagent()

    def test_result_and_status_roundtrip(self):
        slug = "scout"
        record = runtime.new_record("Scout", slug, "compare A and B", "")
        record["status"] = "complete"
        runtime.save_record(slug, record)
        runtime.save_report(slug, "# Verdict\nA wins.")

        fake = MagicMock()
        fake.is_active.return_value = False
        with patch("friday.tools.subagents._runtime", return_value=fake):
            status = subagents.subagent_status("Scout")
            text = subagents.subagent_result("Scout")
            all_agents = subagents.list_subagents()

        self.assertEqual(status["agent"]["status"], "complete")
        self.assertTrue(status["agent"]["has_report"])
        self.assertIn("A wins.", text)
        self.assertEqual(len(all_agents["agents"]), 1)

    def test_subagent_tools_register_with_mcp(self):
        class FakeMcp:
            def __init__(self):
                self.names = []

            def tool(self):
                def decorate(fn):
                    self.names.append(fn.__name__)
                    return fn

                return decorate

        mcp = FakeMcp()
        subagents.register(mcp)
        self.assertEqual(
            mcp.names,
            [
                "propose_subagent",
                "confirm_subagent",
                "cancel_subagent",
                "subagent_status",
                "subagent_result",
                "stop_subagent",
                "list_subagents",
                "check_agent_news",
            ],
        )


if __name__ == "__main__":
    unittest.main()
