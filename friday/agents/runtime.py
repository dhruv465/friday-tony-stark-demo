"""
Executor runtime — background worker agents with tiered tool access.

A worker is a one-shot LLM agent FRIDAY creates (with the boss's
confirmation, or instantly under trust mode) for a delegated task. Every
worker gets the Tier 0 research toolset: web search, page fetch,
knowledge recall, scoped file reads. A worker deployed while trust mode
is armed additionally gets Tier 1 hands — shell commands (through the
hardened broker), scoped file writes, local notes/reminders — and the
Tier 2 ``send_message`` staging tool, which only ever stages a message
for the boss to confirm; it can never send on its own.

If trust mode expires while a trusted job is mid-flight, the job parks
as ``paused_awaiting_trust`` and polls; it resumes when the boss
re-arms, and fails after a bounded wait. Delegation never outlives the
permission that authorized it.

Same threading model as ``friday.learning.engine``: a singleton daemon
thread owning a private asyncio loop, because the desktop Brain executes
MCP tools under throwaway ``asyncio.run`` loops. State lives on disk at
``<FRIDAY_KNOWLEDGE_DIR>/_agents/<slug>/`` (``agent.json`` + ``report.md``)
so status and results survive restarts; ``agent.json`` is re-read between
steps, which makes ``stop`` work from either process.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable

import httpx

from friday.config import config
from friday.learning import extract
from friday.learning.store import knowledge_root
from friday.memory import vault
from friday.security import trust
from friday.tools import web


logger = logging.getLogger("friday.agents")

AGENT_JSON = "agent.json"
REPORT_MD = "report.md"
PREV_REPORT_MD = "last_report.md"
OUTCOME_MD = "outcome.md"
NOTIFY_JSON = "notify.json"
WORK_DIR = "work"
ACTIVE_STALE_S = 120

JOB_TYPES = ("quick", "deep")

PAUSE_POLL_S = 5.0
MAX_FILE_READ_CHARS = 24_000
MAX_FILE_WRITE_CHARS = 200_000
MIN_MONITOR_MINUTES = 1

WORKER_PROMPT = (
    "You are {name}, a focused single-task agent created by {persona} "
    "({boss}'s AI) to handle one delegated job. Work the task step by "
    "step with your tools. Be factual and cite source URLs inline. When the "
    "task is done, call write_report exactly once with a complete, "
    "self-contained markdown report (the boss reads only this report). "
    "If the task is impossible, write_report explaining exactly why."
)

TRUSTED_PROMPT_EXTRA = (
    "\n\nTrust mode is armed: you also have hands — run_shell for "
    "commands (no sudo, no destructive commands; they are blocked), "
    "write_file for files in your workspace, create_note / create_reminder "
    "on the boss's Mac, and send_message which only STAGES a message for "
    "the boss to confirm — it never sends by itself. Use the lightest tool "
    "that does the job and put everything you produced in the report."
)


class TrustLapsed(Exception):
    """Raised by a Tier 1 tool when trust mode expired mid-job."""


# ---------------------------------------------------------------------------
# Knobs
# ---------------------------------------------------------------------------

def _model() -> str:
    return os.getenv("FRIDAY_SUBAGENT_MODEL", "gpt-4o-mini")


def _max_steps(job_type: str = "quick") -> int:
    legacy = os.getenv("FRIDAY_SUBAGENT_MAX_STEPS", "").strip()
    if legacy:
        return int(legacy)
    if job_type == "deep":
        return int(os.getenv("FRIDAY_EXECUTOR_MAX_STEPS_DEEP", "40"))
    return int(os.getenv("FRIDAY_EXECUTOR_MAX_STEPS_QUICK", "15"))


def _max_concurrent() -> int:
    return int(os.getenv("FRIDAY_SUBAGENT_MAX_CONCURRENT", "2"))


def _pause_max_s() -> float:
    return float(os.getenv("FRIDAY_EXECUTOR_PAUSE_MAX_S", "600"))


def _scheduler_tick_s() -> float:
    return float(os.getenv("FRIDAY_SCHEDULER_TICK_S", "30"))


def emit_activity(detail: str, kind: str = "info") -> None:
    try:
        from friday.desktop.events import append_event

        append_event(
            "activity", source="subagent", tool="subagent", detail=detail[:180], kind=kind
        )
    except Exception as exc:
        logger.debug("subagent activity emit skipped: %s", exc)


# ---------------------------------------------------------------------------
# Disk state
# ---------------------------------------------------------------------------

def agents_root() -> Path:
    root = knowledge_root() / "_agents"
    root.mkdir(parents=True, exist_ok=True)
    return root


def slug_for(name: str) -> str:
    return vault.slugify(name)


def agent_dir(slug: str) -> Path:
    if not slug:
        raise ValueError("empty agent slug")
    root = agents_root()
    candidate = (root / slug).resolve()
    rel = candidate.relative_to(root)  # raises ValueError on escape
    if len(rel.parts) != 1:
        raise ValueError(f"agent slug must be a single folder name: {slug!r}")
    candidate.mkdir(parents=True, exist_ok=True)
    return candidate


def work_dir(slug: str) -> Path:
    folder = agent_dir(slug) / WORK_DIR
    folder.mkdir(parents=True, exist_ok=True)
    return folder


def _atomic_write(path: Path, text: str) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def validate_schedule(schedule: dict | None) -> dict | None:
    """Normalize a schedule: None (run now), {"at": iso} (delayed one-shot),
    or {"every_minutes": n} (recurring monitor)."""
    if not schedule:
        return None
    if "at" in schedule:
        from datetime import datetime

        ts = datetime.fromisoformat(str(schedule["at"])).timestamp()  # raises on junk
        return {"at": str(schedule["at"]), "at_ts": ts}
    if "every_minutes" in schedule:
        minutes = int(schedule["every_minutes"])
        if minutes < MIN_MONITOR_MINUTES:
            raise ValueError(f"every_minutes must be ≥ {MIN_MONITOR_MINUTES}")
        return {"every_minutes": minutes}
    raise ValueError("schedule must contain 'at' or 'every_minutes'")


def new_record(
    name: str,
    slug: str,
    task: str,
    reason: str,
    job_type: str = "quick",
    trusted: bool = False,
    schedule: dict | None = None,
) -> dict:
    now = vault.now_iso()
    return {
        "name": name,
        "slug": slug,
        "task": task,
        "reason": reason,
        # scheduled | running | paused_awaiting_trust | complete | failed | stopped
        "status": "running",
        "created_at": now,
        "updated_at": now,
        "steps_taken": 0,
        "model": _model(),
        "job_type": job_type if job_type in JOB_TYPES else "quick",
        "trusted": trusted,
        "schedule": schedule,
        "next_run_ts": None,
        "runs_completed": 0,
        "error": None,
    }


def load_record(slug: str) -> dict | None:
    path = agent_dir(slug) / AGENT_JSON
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def save_record(slug: str, record: dict) -> None:
    record["updated_at"] = vault.now_iso()
    _atomic_write(agent_dir(slug) / AGENT_JSON, json.dumps(record, indent=2))


def _save_preserving_stop(slug: str, record: dict) -> None:
    """Save worker progress without clobbering a stop the boss wrote to
    disk while an LLM step was in flight."""
    disk = load_record(slug)
    if disk and disk.get("status") == "stopped" and record.get("status") in (
        "running",
        "paused_awaiting_trust",
    ):
        record["status"] = "stopped"
    save_record(slug, record)


def save_report(slug: str, markdown: str) -> None:
    _atomic_write(agent_dir(slug) / REPORT_MD, markdown)


def read_report(slug: str) -> str:
    path = agent_dir(slug) / REPORT_MD
    return path.read_text(encoding="utf-8") if path.is_file() else ""


def list_records() -> list[dict]:
    records = []
    for path in sorted(agents_root().glob(f"*/{AGENT_JSON}")):
        try:
            records.append(json.loads(path.read_text(encoding="utf-8")))
        except (json.JSONDecodeError, OSError):
            continue
    records.sort(key=lambda r: r.get("updated_at") or "", reverse=True)
    return records


# ---------------------------------------------------------------------------
# File scoping
# ---------------------------------------------------------------------------

def _file_roots(slug: str) -> list[Path]:
    """Directories a worker may read/write: its own work folder plus any
    boss-whitelisted roots from FRIDAY_EXECUTOR_FILE_ROOTS."""
    roots = [work_dir(slug)]
    raw = os.getenv("FRIDAY_EXECUTOR_FILE_ROOTS", "").strip()
    for part in raw.split(","):
        part = part.strip()
        if part:
            roots.append(Path(part).expanduser().resolve())
    return roots


def _resolve_in_roots(slug: str, raw_path: str) -> Path:
    """Resolve a worker-supplied path inside the allowed roots.

    Relative paths land in the worker's work folder. Absolute paths must
    fall under a whitelisted root. Symlinks are resolved before the
    containment check.
    """
    roots = _file_roots(slug)
    candidate = Path(raw_path).expanduser()
    if not candidate.is_absolute():
        candidate = roots[0] / candidate
    resolved = candidate.resolve()
    for root in roots:
        try:
            resolved.relative_to(root)
            return resolved
        except ValueError:
            continue
    raise PermissionError(f"path outside the allowed workspace: {raw_path!r}")


# ---------------------------------------------------------------------------
# Tool registry
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ExecutorTool:
    name: str
    description: str
    parameters: dict
    impl: Callable[..., Awaitable[str]]
    tier: int


def _schema(tool: ExecutorTool) -> dict:
    return {
        "type": "function",
        "function": {
            "name": tool.name,
            "description": tool.description,
            "parameters": tool.parameters,
        },
    }


def _str_params(**props: str) -> dict:
    return {
        "type": "object",
        "properties": {name: {"type": "string", "description": desc} for name, desc in props.items()},
        "required": list(props),
    }


async def _tool_web_search(client: httpx.AsyncClient, slug: str, args: dict) -> str:
    results = await web.ddg_search_raw(str(args.get("query", "")), 6)
    if not results:
        return "No results."
    return "\n".join(
        f"{i}. {title}\n   {snippet}\n   {link}"
        for i, (title, link, snippet) in enumerate(results, 1)
    )


async def _tool_fetch_page(client: httpx.AsyncClient, slug: str, args: dict) -> str:
    return await extract.fetch_page_text(client, str(args.get("url", "")), max_chars=8000)


async def _tool_recall_knowledge(client: httpx.AsyncClient, slug: str, args: dict) -> str:
    from friday.tools.learning import recall_knowledge

    return recall_knowledge(str(args.get("query", "")))


async def _tool_read_file(client: httpx.AsyncClient, slug: str, args: dict) -> str:
    path = _resolve_in_roots(slug, str(args.get("path", "")))
    if not path.is_file():
        return f"No such file: {path}"
    text = path.read_text(encoding="utf-8", errors="replace")
    if len(text) > MAX_FILE_READ_CHARS:
        text = text[:MAX_FILE_READ_CHARS] + f"\n…[truncated {len(text) - MAX_FILE_READ_CHARS} chars]"
    return text


async def _tool_write_file(client: httpx.AsyncClient, slug: str, args: dict) -> str:
    content = str(args.get("content", ""))
    if len(content) > MAX_FILE_WRITE_CHARS:
        return f"Refused: content over {MAX_FILE_WRITE_CHARS} chars."
    path = _resolve_in_roots(slug, str(args.get("path", "")))
    path.parent.mkdir(parents=True, exist_ok=True)
    _atomic_write(path, content)
    return f"Wrote {len(content)} chars to {path}"


def _trusted_result(result: dict, kind: str) -> str:
    """Interpret a broker response under trust mode. ``pending_confirmation``
    means trust lapsed between the loop's check and the call — park the job
    rather than leaving a stale staged action behind."""
    status = result.get("status", "")
    if status == "pending_confirmation":
        raise TrustLapsed(f"{kind} needs confirmation — trust mode is no longer armed")
    return json.dumps({k: v for k, v in result.items() if k != "trust_mode"})


async def _tool_run_shell(client: httpx.AsyncClient, slug: str, args: dict) -> str:
    from friday.tools import shell

    command = str(args.get("command", ""))
    reason = str(args.get("reason", ""))
    # propose_shell validates (denylist, allowlist, secrets) and, with
    # trust armed, executes immediately. All hardening stays in one place.
    result = await asyncio.to_thread(shell.propose_shell, command, reason)
    if result.get("status") == "pending_confirmation":
        await asyncio.to_thread(shell.cancel_shell, result.get("action_id"))
        raise TrustLapsed("run_shell needs confirmation — trust mode is no longer armed")
    return _trusted_result(result, "run_shell")


async def _tool_create_note(client: httpx.AsyncClient, slug: str, args: dict) -> str:
    from friday.tools import local_apps

    result = await asyncio.to_thread(
        local_apps.prepare_local_note,
        str(args.get("title", "")),
        str(args.get("body", "")),
    )
    if result.get("status") == "pending_confirmation":
        await asyncio.to_thread(local_apps.cancel_local_app_action, result.get("action_id"))
        raise TrustLapsed("create_note needs confirmation — trust mode is no longer armed")
    return _trusted_result(result, "create_note")


async def _tool_create_reminder(client: httpx.AsyncClient, slug: str, args: dict) -> str:
    from friday.tools import local_apps

    result = await asyncio.to_thread(
        local_apps.prepare_local_reminder,
        str(args.get("title", "")),
        str(args.get("notes", "")),
    )
    if result.get("status") == "pending_confirmation":
        await asyncio.to_thread(local_apps.cancel_local_app_action, result.get("action_id"))
        raise TrustLapsed("create_reminder needs confirmation — trust mode is no longer armed")
    return _trusted_result(result, "create_reminder")


async def _tool_send_message(client: httpx.AsyncClient, slug: str, args: dict) -> str:
    """Tier 2: stages an outbound message through the messaging broker.
    The boss must confirm it by voice — this tool can never send."""
    from friday.tools import messaging

    result = await asyncio.to_thread(
        messaging.prepare_outbound_message,
        str(args.get("channel", "")),
        str(args.get("recipient", "")),
        str(args.get("message", "")),
    )
    if result.get("status") == "pending_confirmation":
        return (
            "Message staged for the boss to confirm — it will NOT send unless "
            "he approves it by voice. Note this in your report. "
            + json.dumps({"summary": result.get("summary", ""), "channel": result.get("channel", "")})
        )
    return json.dumps(result)


TIER0_TOOLS: tuple[ExecutorTool, ...] = (
    ExecutorTool(
        "web_search",
        "Search the web (DuckDuckGo). Returns titles, URLs, snippets.",
        _str_params(query="search query"),
        _tool_web_search,
        trust.TIER_FREE,
    ),
    ExecutorTool(
        "fetch_page",
        "Fetch a web page and return its readable text.",
        _str_params(url="page URL"),
        _tool_fetch_page,
        trust.TIER_FREE,
    ),
    ExecutorTool(
        "recall_knowledge",
        "Look up FRIDAY's learned knowledge notes on a topic.",
        _str_params(query="topic"),
        _tool_recall_knowledge,
        trust.TIER_FREE,
    ),
    ExecutorTool(
        "read_file",
        "Read a text file from your workspace (or a whitelisted folder).",
        _str_params(path="file path, relative paths land in your work folder"),
        _tool_read_file,
        trust.TIER_FREE,
    ),
)

TIER1_TOOLS: tuple[ExecutorTool, ...] = (
    ExecutorTool(
        "run_shell",
        "Run one shell command (no pipes/chaining, no sudo, 20s timeout). "
        "Destructive commands are blocked.",
        _str_params(command="single shell command", reason="why this command"),
        _tool_run_shell,
        trust.TIER_TRUSTED,
    ),
    ExecutorTool(
        "write_file",
        "Write a text file in your workspace (or a whitelisted folder).",
        _str_params(path="file path", content="full file content"),
        _tool_write_file,
        trust.TIER_TRUSTED,
    ),
    ExecutorTool(
        "create_note",
        "Create an Apple Notes note on the boss's Mac.",
        _str_params(title="note title", body="note body"),
        _tool_create_note,
        trust.TIER_TRUSTED,
    ),
    ExecutorTool(
        "create_reminder",
        "Create an Apple Reminders item on the boss's Mac.",
        _str_params(title="reminder title", notes="extra notes"),
        _tool_create_reminder,
        trust.TIER_TRUSTED,
    ),
    ExecutorTool(
        "send_message",
        "STAGE an outbound message (imessage/whatsapp/slack/email) for the "
        "boss to confirm by voice. Never sends on its own.",
        _str_params(channel="imessage|whatsapp|slack|email", recipient="who", message="text"),
        _tool_send_message,
        trust.TIER_ALWAYS_CONFIRM,
    ),
)

WRITE_REPORT_SCHEMA = {
    "type": "function",
    "function": {
        "name": "write_report",
        "description": "Submit the final markdown report. Ends the task.",
        "parameters": _str_params(report_markdown="complete markdown report"),
    },
}


def _registry_for(record: dict) -> dict[str, ExecutorTool]:
    tools = list(TIER0_TOOLS)
    if record.get("trusted"):
        tools.extend(TIER1_TOOLS)
    return {tool.name: tool for tool in tools}


def _tools_payload(registry: dict[str, ExecutorTool]) -> list[dict]:
    return [_schema(tool) for tool in registry.values()] + [WRITE_REPORT_SCHEMA]


def _worker_prompt(record: dict, lessons: str = "") -> str:
    prompt = WORKER_PROMPT.format(
        name=record["name"], persona=config.PERSONA_NAME, boss=config.PERSONA_BOSS
    )
    if record.get("trusted"):
        prompt += TRUSTED_PROMPT_EXTRA
    if lessons:
        prompt += "\n\n" + lessons
    return prompt


# ---------------------------------------------------------------------------
# Learning hooks — outcome notes + lessons from past jobs
# ---------------------------------------------------------------------------

def write_outcome(slug: str, record: dict) -> None:
    """Persist a searchable outcome note for this job. Lives inside the
    knowledge root, so ``store.recall`` (and future workers) can find it."""
    report = read_report(slug)
    excerpt = report[:600] + ("…" if len(report) > 600 else "")
    body = "\n".join(
        [
            f"# Agent outcome — {record['name']}",
            "",
            f"- task: {record['task']}",
            f"- status: {record['status']}",
            f"- job_type: {record.get('job_type', 'quick')}",
            f"- trusted: {record.get('trusted', False)}",
            f"- steps_taken: {record.get('steps_taken', 0)}",
            f"- error: {record.get('error') or 'none'}",
            f"- finished_at: {vault.now_iso()}",
            "",
            "## Report excerpt",
            "",
            excerpt or "(no report)",
            "",
        ]
    )
    _atomic_write(agent_dir(slug) / OUTCOME_MD, body)


def _lessons_for(task: str, own_slug: str) -> str:
    """Recall outcome notes from similar past jobs and format them as
    prompt guidance. Only outcome notes count — general knowledge is
    available to the worker through its recall_knowledge tool."""
    try:
        from friday.learning import store

        hits = [
            h
            for h in store.recall(task, k=10)
            if h.path.endswith(OUTCOME_MD) and f"/{own_slug}/" not in h.path
        ][:3]
    except Exception as exc:
        logger.debug("lesson recall skipped: %s", exc)
        return ""
    if not hits:
        return ""
    blocks = "\n\n".join(f"### {h.title}\n{h.snippet}" for h in hits)
    return (
        "Lessons from your past jobs on similar tasks (what worked, what "
        "failed — use them, don't repeat mistakes):\n\n" + blocks
    )


# ---------------------------------------------------------------------------
# Worker loop
# ---------------------------------------------------------------------------

_openai_client = None


async def _chat(messages: list[dict], tools: list[dict], force_tool: str | None = None):
    """One LLM step. Module-level so tests can patch it."""
    global _openai_client
    if _openai_client is None:
        if not config.OPENAI_API_KEY:
            raise RuntimeError("OPENAI_API_KEY is not set — subagents need it.")
        from openai import AsyncOpenAI

        _openai_client = AsyncOpenAI(api_key=config.OPENAI_API_KEY)
    kwargs: dict = {}
    if tools:
        kwargs["tools"] = tools
        if force_tool:
            kwargs["tool_choice"] = {"type": "function", "function": {"name": force_tool}}
    return await _openai_client.chat.completions.create(
        model=_model(), messages=messages, temperature=0.3, **kwargs
    )


async def _execute_tool(
    client: httpx.AsyncClient,
    registry: dict[str, ExecutorTool],
    slug: str,
    name: str,
    args: dict,
) -> str:
    tool = registry.get(name)
    if tool is None:
        return f"Unknown tool: {name}"
    try:
        return await tool.impl(client, slug, args)
    except TrustLapsed:
        raise
    except Exception as exc:
        return f"Tool error: {exc}"


async def _await_trust(slug: str, record: dict) -> bool:
    """Park a trusted job whose trust window lapsed. Returns True when the
    boss re-arms, False when the job is stopped or the wait times out."""
    record["status"] = "paused_awaiting_trust"
    _save_preserving_stop(slug, record)
    if record.get("status") == "stopped":
        return False
    emit_activity(f"agent '{record['name']}' paused — trust mode expired", kind="info")
    deadline = time.monotonic() + _pause_max_s()
    while time.monotonic() < deadline:
        await asyncio.sleep(PAUSE_POLL_S)
        current = load_record(slug) or record
        if current.get("status") == "stopped":
            record["status"] = "stopped"
            return False
        if trust.is_armed():
            record["status"] = "running"
            _save_preserving_stop(slug, record)
            if record.get("status") == "stopped":
                return False
            emit_activity(f"agent '{record['name']}' resumed — trust mode re-armed", kind="ok")
            return True
    record["status"] = "failed"
    record["error"] = "trust mode expired and was not re-armed in time"
    save_record(slug, record)
    emit_activity(f"agent '{record['name']}' failed — trust window closed", kind="err")
    return False


BUDGET_WARNING_MSG = (
    "Heads up: you have very few steps left. Start wrapping up — gather "
    "nothing new unless essential, then call write_report."
)

BUDGET_FINAL_MSG = (
    "STOP. This is your final step — the budget is exhausted. Call "
    "write_report NOW with a complete markdown report of everything you "
    "found so far. If the task is unfinished, state exactly what is done "
    "and what remains."
)


def _partial_report(messages: list[dict]) -> str:
    """Assemble a salvage report from the transcript so the boss still
    gets the worker's findings when no report was ever written."""
    notes = [
        str(m.get("content", ""))
        for m in messages
        if m.get("role") in ("assistant", "tool") and m.get("content")
    ]
    body = "\n\n".join(notes[-8:]).strip()
    if not body:
        return ""
    return (
        "## Partial findings (step budget exhausted before a final report)\n\n"
        + body[:12000]
    )


async def _run_agent(slug: str) -> None:
    record = load_record(slug)
    if record is None:
        return
    emit_activity(f"agent '{record['name']}' deployed — {record['task'][:80]}")
    registry = _registry_for(record)
    tools_payload = _tools_payload(registry)
    lessons = _lessons_for(record["task"], slug)
    messages: list[dict] = [
        {"role": "system", "content": _worker_prompt(record, lessons)},
        {"role": "user", "content": record["task"]},
    ]
    max_steps = _max_steps(record.get("job_type", "quick"))
    try:
        async with httpx.AsyncClient(follow_redirects=True, timeout=15) as client:
            for _step in range(max_steps):
                current = load_record(slug) or record
                if current.get("status") == "stopped":
                    emit_activity(f"agent '{record['name']}' stopped by the boss", kind="info")
                    return
                # A trusted job must not keep its hands past the trust
                # window — park and wait before the next LLM step.
                if record.get("trusted") and not trust.is_armed():
                    if not await _await_trust(slug, record):
                        return
                # Budget management: warn one step early, then force the
                # final step to be write_report so the run always ends
                # with a report instead of dying mid-loop.
                final_step = _step == max_steps - 1
                if final_step:
                    messages.append({"role": "user", "content": BUDGET_FINAL_MSG})
                    response = await _chat(
                        messages, [WRITE_REPORT_SCHEMA], force_tool="write_report"
                    )
                elif _step == max_steps - 2 and max_steps > 2:
                    messages.append({"role": "user", "content": BUDGET_WARNING_MSG})
                    response = await _chat(messages, tools_payload)
                else:
                    response = await _chat(messages, tools_payload)
                message = response.choices[0].message
                record["steps_taken"] += 1
                _save_preserving_stop(slug, record)
                if record.get("status") == "stopped":
                    emit_activity(f"agent '{record['name']}' stopped by the boss")
                    return

                tool_calls = getattr(message, "tool_calls", None) or []
                if not tool_calls:
                    # Model answered in plain text — treat it as the report.
                    save_report(slug, message.content or "(empty report)")
                    record["status"] = "complete"
                    save_record(slug, record)
                    emit_activity(f"agent '{record['name']}' finished — report ready", kind="ok")
                    return

                messages.append(
                    {
                        "role": "assistant",
                        "content": message.content or "",
                        "tool_calls": [
                            {
                                "id": tc.id,
                                "type": "function",
                                "function": {
                                    "name": tc.function.name,
                                    "arguments": tc.function.arguments,
                                },
                            }
                            for tc in tool_calls
                        ],
                    }
                )
                for tc in tool_calls:
                    try:
                        args = json.loads(tc.function.arguments or "{}")
                    except json.JSONDecodeError:
                        args = {}
                    if tc.function.name == "write_report":
                        save_report(slug, str(args.get("report_markdown", "")) or "(empty report)")
                        record["status"] = "complete"
                        save_record(slug, record)
                        emit_activity(
                            f"agent '{record['name']}' finished — report ready", kind="ok"
                        )
                        return
                    try:
                        result = await _execute_tool(client, registry, slug, tc.function.name, args)
                    except TrustLapsed as lapse:
                        messages.append(
                            {"role": "tool", "tool_call_id": tc.id, "content": str(lapse)}
                        )
                        if not await _await_trust(slug, record):
                            return
                        continue
                    messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": tc.id,
                            "content": str(result)[:12000],
                        }
                    )

        # Forced write_report on the final step makes this nearly
        # unreachable, but if it happens the transcript is still saved
        # as a partial report so the findings aren't lost.
        partial = _partial_report(messages)
        if partial:
            save_report(slug, partial)
        record["status"] = "failed"
        record["error"] = f"step limit ({max_steps}) reached without a report" + (
            " — partial findings saved" if partial else ""
        )
        save_record(slug, record)
        emit_activity(f"agent '{record['name']}' hit its step limit", kind="err")
    except Exception as exc:
        logger.exception("subagent %r crashed", slug)
        record["status"] = "failed"
        record["error"] = str(exc)
        save_record(slug, record)
        emit_activity(f"agent '{record['name']}' failed — {exc}", kind="err")


# ---------------------------------------------------------------------------
# Scheduler + monitors
# ---------------------------------------------------------------------------

MONITOR_DIFF_PROMPT = (
    "You compare two reports from a recurring monitoring agent. Answer with "
    "exactly one word: CHANGED if the new report contains materially new or "
    "different information the boss would want to hear about, UNCHANGED if "
    "it is substantively the same situation."
)


async def _monitor_changed(prev: str, new: str) -> bool:
    """Decide whether a monitor's new report is worth announcing.
    First report is always news; on LLM failure, err on announcing."""
    if not prev.strip():
        return True
    if prev.strip() == new.strip():
        return False
    try:
        response = await _chat(
            [
                {"role": "system", "content": MONITOR_DIFF_PROMPT},
                {
                    "role": "user",
                    "content": f"PREVIOUS REPORT:\n{prev[:6000]}\n\nNEW REPORT:\n{new[:6000]}",
                },
            ],
            tools=[],
        )
        verdict = (response.choices[0].message.content or "").strip().upper()
        return "UNCHANGED" not in verdict
    except Exception as exc:
        logger.debug("monitor diff LLM failed (%s) — defaulting to changed", exc)
        return True


def _mac_notification(title: str, body: str) -> None:
    import subprocess

    from friday.tools.local_apps import _apple_string

    try:
        subprocess.run(
            [
                "osascript",
                "-e",
                f'display notification "{_apple_string(body[:160])}" '
                f'with title "{_apple_string(title[:60])}"',
            ],
            capture_output=True,
            timeout=5,
            check=False,
        )
    except Exception as exc:
        logger.debug("mac notification skipped: %s", exc)


def set_pending_notify(slug: str, detail: str) -> None:
    _atomic_write(
        agent_dir(slug) / NOTIFY_JSON,
        json.dumps({"pending": True, "detail": detail[:300], "at": vault.now_iso()}),
    )


def pop_pending_notifies() -> list[dict]:
    """Collect and clear every agent's pending notify flag. The voice agent
    calls this at interaction time to mention finished work."""
    pending = []
    for path in sorted(agents_root().glob(f"*/{NOTIFY_JSON}")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            data = None
        if data and data.get("pending"):
            data["slug"] = path.parent.name
            pending.append(data)
        try:
            path.unlink()
        except FileNotFoundError:
            pass
    return pending


def _notify(slug: str, record: dict, detail: str) -> None:
    emit_activity(detail, kind="ok")
    _mac_notification(f"FRIDAY — {record['name']}", detail)
    set_pending_notify(slug, detail)


# ---------------------------------------------------------------------------
# Proactive event alerts (temporal facts)
# ---------------------------------------------------------------------------

_EVENTS_PSEUDO_SLUG = "events"
_last_groom_ts = 0.0


def _alert_line(event: dict) -> str:
    content = event["content"]
    window = event["window"]
    if window == "now":
        return f"Reminder: {content} is starting now."
    if window == "1h":
        return f"Reminder: {content} in about an hour."
    return f"Reminder: {content} in {round(event['minutes_until'])} minutes."


def check_event_alerts() -> None:
    """One sweep of the temporal-facts alert windows. Fires a macOS
    notification immediately and parks a pending notify so the voice
    agent mentions it on the next exchange. Never raises — this runs
    inside the scheduler tick."""
    global _last_groom_ts
    try:
        from friday.memory import facts

        now_ts = time.time()
        if now_ts - _last_groom_ts > 3600:
            _last_groom_ts = now_ts
            facts.groom_expired()

        for event in facts.events_due_for_alert():
            line = _alert_line(event)
            _mac_notification("FRIDAY — reminder", line)
            set_pending_notify(_EVENTS_PSEUDO_SLUG, line)
            emit_activity(line, kind="ok")
            facts.mark_alerted(event["id"], event["window"])
    except Exception as exc:
        logger.debug("event alert sweep skipped: %s", exc)


async def _run_agent_job(slug: str) -> None:
    """One scheduled/immediate run of an agent, plus monitor bookkeeping."""
    record = load_record(slug)
    if record is None:
        return
    is_monitor = bool((record.get("schedule") or {}).get("every_minutes"))
    prev_report = read_report(slug) if is_monitor else ""
    if is_monitor and prev_report:
        _atomic_write(agent_dir(slug) / PREV_REPORT_MD, prev_report)
        # Each monitor run starts a fresh conversation and report.
        record["steps_taken"] = 0
        record["error"] = None
        save_record(slug, record)

    await _run_agent(slug)

    record = load_record(slug)
    if record is None:
        return
    record["runs_completed"] = int(record.get("runs_completed", 0)) + 1

    if record["status"] in ("complete", "failed"):
        try:
            write_outcome(slug, record)
        except Exception as exc:
            logger.debug("outcome note skipped: %s", exc)

    if record["status"] == "complete":
        if is_monitor:
            new_report = read_report(slug)
            if await _monitor_changed(prev_report, new_report):
                _notify(slug, record, f"monitor '{record['name']}' has news — report updated")
        else:
            _notify(slug, record, f"agent '{record['name']}' finished — report ready")

    # Recurring monitors go back on the calendar unless the boss stopped
    # them; failed runs retry on the same cadence.
    if is_monitor and record["status"] in ("complete", "failed"):
        every_s = int(record["schedule"]["every_minutes"]) * 60
        record["status"] = "scheduled"
        record["next_run_ts"] = time.time() + every_s
    save_record(slug, record)


# ---------------------------------------------------------------------------
# Runtime
# ---------------------------------------------------------------------------

class SubagentRuntime:
    """Singleton daemon thread + private event loop for subagent jobs."""

    _instance: "SubagentRuntime | None" = None
    _instance_lock = threading.Lock()

    def __init__(self) -> None:
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._futures: dict[str, "asyncio.Future"] = {}
        self._semaphore: asyncio.Semaphore | None = None
        self._scheduler_running = False

    @classmethod
    def instance(cls) -> "SubagentRuntime":
        with cls._instance_lock:
            if cls._instance is None:
                cls._instance = cls()
            return cls._instance

    def ensure_running(self) -> None:
        """Start the daemon loop + scheduler without deploying a job —
        used by server boot so event alerts fire from minute one."""
        self._ensure_loop()

    def _ensure_loop(self) -> asyncio.AbstractEventLoop:
        if self._loop is not None and self._thread is not None and self._thread.is_alive():
            return self._loop
        loop = asyncio.new_event_loop()

        def _runner() -> None:
            asyncio.set_event_loop(loop)
            loop.run_forever()

        thread = threading.Thread(target=_runner, name="friday-subagents", daemon=True)
        thread.start()
        self._loop = loop
        self._thread = thread
        self._semaphore = asyncio.Semaphore(_max_concurrent())
        self._scheduler_running = False
        self._ensure_scheduler()
        return loop

    def _ensure_scheduler(self) -> None:
        if self._scheduler_running or self._loop is None:
            return
        self._scheduler_running = True
        asyncio.run_coroutine_threadsafe(self._scheduler_loop(), self._loop)

    async def _scheduler_loop(self) -> None:
        """Tick: launch every scheduled job whose time has come. Flipping
        status to running before launch makes double-fires impossible —
        the next tick no longer sees the record as scheduled."""
        while True:
            await asyncio.sleep(_scheduler_tick_s())
            try:
                self._tick(time.time())
            except Exception:
                logger.exception("scheduler tick failed")
            check_event_alerts()

    def _tick(self, now: float) -> None:
        for record in list_records():
            if record.get("status") != "scheduled":
                continue
            next_ts = record.get("next_run_ts")
            if next_ts is None or float(next_ts) > now:
                continue
            slug = record["slug"]
            if self.is_active(slug):
                continue
            record["status"] = "running"
            save_record(slug, record)
            self._futures[slug] = asyncio.ensure_future(self._guarded(slug))

    async def _guarded(self, slug: str) -> None:
        assert self._semaphore is not None
        async with self._semaphore:
            await _run_agent_job(slug)

    def is_active(self, slug: str) -> bool:
        future = self._futures.get(slug)
        return future is not None and not future.done()

    def start_agent(
        self,
        name: str,
        task: str,
        reason: str = "",
        job_type: str = "quick",
        schedule: dict | None = None,
    ) -> dict:
        slug = slug_for(name)
        if self.is_active(slug):
            return {"status": "already_active", "name": name, "slug": slug}
        schedule = validate_schedule(schedule)
        # Trust is sampled once, at deploy time: a job deployed under
        # trust gets Tier 1 hands (and pauses if the window closes);
        # a job deployed without it stays research-only for its lifetime.
        record = new_record(
            name,
            slug,
            task,
            reason,
            job_type=job_type,
            trusted=trust.is_armed(),
            schedule=schedule,
        )
        loop = self._ensure_loop()
        if schedule and "at_ts" in schedule:
            # Delayed one-shot: parked until the scheduler's tick.
            record["status"] = "scheduled"
            record["next_run_ts"] = schedule["at_ts"]
            save_record(slug, record)
        else:
            # Run-now jobs and monitors both fire immediately; a monitor's
            # next run is put on the calendar when this one finishes.
            save_record(slug, record)
            self._futures[slug] = asyncio.run_coroutine_threadsafe(self._guarded(slug), loop)
        return {
            "status": "agent_deployed" if record["status"] == "running" else "agent_scheduled",
            "name": name,
            "slug": slug,
            "trusted": record["trusted"],
            "job_type": record["job_type"],
            "schedule": record["schedule"],
        }

    def request_stop(self, slug: str) -> dict:
        record = load_record(slug)
        if record is None:
            return {"status": "unknown_agent", "slug": slug}
        if record["status"] in ("running", "paused_awaiting_trust", "scheduled"):
            record["status"] = "stopped"
            save_record(slug, record)
        return {"status": record["status"], "name": record["name"], "slug": slug}
