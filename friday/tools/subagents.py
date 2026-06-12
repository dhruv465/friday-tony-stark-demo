"""Subagent MCP tools — propose / confirm broker + status + results.

FRIDAY never spawns an agent silently: it proposes the agent with the
task and WHY it wants one, the boss confirms verbally, then the worker
runs in the background (``friday.agents.runtime``) and leaves a markdown
report. Same broker shape as ``friday.tools.shell`` / ``friday.tools.learning``.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass


PENDING_TTL_SECONDS = 120
PENDING_SUBAGENT_ACTIONS: dict[str, "PendingSubagent"] = {}

MAX_TASK_CHARS = 600
MAX_NAME_CHARS = 60


JOB_TYPES = ("quick", "deep")


@dataclass
class PendingSubagent:
    action_id: str
    name: str
    task: str
    reason: str
    created_at: float
    job_type: str = "quick"
    schedule: dict | None = None


def _prune_expired() -> None:
    now = time.time()
    expired = [
        aid for aid, act in PENDING_SUBAGENT_ACTIONS.items()
        if now - act.created_at > PENDING_TTL_SECONDS
    ]
    for aid in expired:
        PENDING_SUBAGENT_ACTIONS.pop(aid, None)


def _latest_pending_id() -> str:
    _prune_expired()
    if not PENDING_SUBAGENT_ACTIONS:
        raise KeyError("No pending subagent proposal.")
    return max(
        PENDING_SUBAGENT_ACTIONS.values(), key=lambda a: a.created_at
    ).action_id


def clear_pending_subagent_actions() -> None:
    PENDING_SUBAGENT_ACTIONS.clear()


def _runtime():
    from friday.agents.runtime import SubagentRuntime

    return SubagentRuntime.instance()


def _build_schedule(schedule_at: str, every_minutes: int) -> dict | None:
    if schedule_at and every_minutes:
        raise ValueError("Pick one: schedule_at (run once later) or every_minutes (recurring).")
    if schedule_at:
        return {"at": schedule_at.strip()}
    if every_minutes:
        return {"every_minutes": int(every_minutes)}
    return None


def propose_subagent(
    task: str,
    name: str = "",
    reason: str = "",
    job_type: str = "quick",
    schedule_at: str = "",
    every_minutes: int = 0,
) -> dict:
    task = " ".join(task.split()).strip()
    if not task:
        raise ValueError("Empty task.")
    if len(task) > MAX_TASK_CHARS:
        raise ValueError(f"Task too long — keep it under {MAX_TASK_CHARS} characters.")
    if job_type not in JOB_TYPES:
        raise ValueError(f"job_type must be one of {JOB_TYPES}.")

    from friday.agents import runtime

    schedule = runtime.validate_schedule(_build_schedule(schedule_at, every_minutes))
    name = " ".join(name.split()).strip() or task[:40]
    if len(name) > MAX_NAME_CHARS:
        name = name[:MAX_NAME_CHARS]

    from friday.agents import runtime

    slug = runtime.slug_for(name)
    existing = runtime.load_record(slug)
    if existing and (existing.get("status") == "running" or _runtime().is_active(slug)):
        return {
            "status": "already_running",
            "name": existing["name"],
            "slug": slug,
            "task": existing["task"],
        }

    action_id = uuid.uuid4().hex
    PENDING_SUBAGENT_ACTIONS[action_id] = PendingSubagent(
        action_id=action_id,
        name=name,
        task=task,
        reason=reason.strip(),
        created_at=time.time(),
        job_type=job_type,
        schedule=schedule,
    )
    # Tier 1: deploying a worker agent — trust mode deploys immediately.
    from friday.security import trust

    trusted = trust.trusted_short_circuit(confirm_subagent, action_id)
    if trusted is not None:
        return trusted

    return {
        "status": "pending_confirmation",
        "action_id": action_id,
        "name": name,
        "task": task,
        "reason": reason.strip(),
        "job_type": job_type,
        "schedule": schedule,
        "expires_in_seconds": PENDING_TTL_SECONDS,
    }


def confirm_subagent(action_id: str | None = None) -> dict:
    if not action_id:
        action_id = _latest_pending_id()
    action = PENDING_SUBAGENT_ACTIONS.get(action_id)
    if action is None:
        raise KeyError(f"No pending subagent proposal: {action_id}")
    if time.time() - action.created_at > PENDING_TTL_SECONDS:
        PENDING_SUBAGENT_ACTIONS.pop(action_id, None)
        raise TimeoutError(f"Pending subagent proposal expired: {action_id}")
    PENDING_SUBAGENT_ACTIONS.pop(action_id, None)
    return _runtime().start_agent(
        action.name,
        action.task,
        action.reason,
        job_type=action.job_type,
        schedule=action.schedule,
    )


def cancel_subagent(action_id: str | None = None) -> dict:
    if not action_id:
        try:
            action_id = _latest_pending_id()
        except KeyError:
            return {"status": "noop", "reason": "no pending subagent proposal"}
    removed = PENDING_SUBAGENT_ACTIONS.pop(action_id, None)
    return {"status": "cancelled" if removed else "noop", "action_id": action_id}


def subagent_status(name: str = "") -> dict:
    from friday.agents import runtime

    if not name.strip():
        records = runtime.list_records()
        for record in records:
            record["active_now"] = _runtime().is_active(record["slug"])
        return {"status": "ok", "agents": records}
    slug = runtime.slug_for(name)
    record = runtime.load_record(slug)
    if record is None:
        return {"status": "unknown_agent", "name": name}
    record["active_now"] = _runtime().is_active(slug)
    record["has_report"] = bool(runtime.read_report(slug))
    return {"status": "ok", "agent": record}


def subagent_result(name: str) -> str:
    from friday.agents import runtime

    slug = runtime.slug_for(name)
    record = runtime.load_record(slug)
    if record is None:
        return f"No agent named {name!r}, boss."
    report = runtime.read_report(slug)
    if not report:
        return (
            f"Agent {record['name']} is {record['status']} — no report yet."
            + (f" Error: {record['error']}" if record.get("error") else "")
        )
    return f"### AGENT REPORT — {record['name']} ({record['status']})\n\n{report}"


def stop_subagent(name: str) -> dict:
    from friday.agents import runtime

    return _runtime().request_stop(runtime.slug_for(name))


def list_subagents() -> dict:
    from friday.agents import runtime

    return {
        "status": "ok",
        "agents": [
            {
                "name": r["name"],
                "slug": r["slug"],
                "status": r["status"],
                "task": r["task"][:80],
                "updated_at": r["updated_at"],
            }
            for r in runtime.list_records()
        ],
    }


def register(mcp):
    @mcp.tool()
    def propose_subagent(
        task: str,
        name: str = "",
        reason: str = "",
        job_type: str = "quick",
        schedule_at: str = "",
        every_minutes: int = 0,
    ) -> dict:
        """
        Stage a background subagent for the boss to confirm. Use when the
        boss delegates a self-contained task or says "create an agent for
        X". reason must say WHY an agent helps (runs in background,
        multi-step digging). job_type "quick" for focused lookups,
        "deep" for big multi-step jobs (larger step budget).
        Scheduling: schedule_at (ISO datetime, e.g. "2026-06-11T18:00")
        runs it once later; every_minutes makes it a recurring monitor
        ("keep an eye on X") that re-runs and only pings the boss when
        something actually changed. Agents deployed while trust mode is
        armed also get hands: shell, file writes, notes/reminders, and
        message STAGING (the boss still confirms sends). Without trust
        mode they only research, and deployment itself needs the boss's
        confirmation.
        """
        import friday.tools.subagents as _self

        return _self.propose_subagent(
            task=task,
            name=name,
            reason=reason,
            job_type=job_type,
            schedule_at=schedule_at,
            every_minutes=every_minutes,
        )

    @mcp.tool()
    def confirm_subagent(action_id: str | None = None) -> dict:
        """
        Deploy the most recently proposed subagent. It works in the
        background (web search, page reading, knowledge recall) and leaves
        a markdown report. Only call after the boss confirms.
        """
        import friday.tools.subagents as _self

        return _self.confirm_subagent(action_id=action_id)

    @mcp.tool()
    def cancel_subagent(action_id: str | None = None) -> dict:
        """Drop a pending subagent proposal without deploying it."""
        import friday.tools.subagents as _self

        return _self.cancel_subagent(action_id=action_id)

    @mcp.tool()
    def subagent_status(name: str = "") -> dict:
        """
        Check on deployed subagents. Empty name → all agents with status;
        a specific name → full record + whether its report is ready.
        """
        import friday.tools.subagents as _self

        return _self.subagent_status(name=name)

    @mcp.tool()
    def subagent_result(name: str) -> str:
        """Fetch a finished subagent's markdown report to summarize aloud."""
        import friday.tools.subagents as _self

        return _self.subagent_result(name=name)

    @mcp.tool()
    def stop_subagent(name: str) -> dict:
        """Stop a running subagent. Its partial state is kept."""
        import friday.tools.subagents as _self

        return _self.stop_subagent(name=name)

    @mcp.tool()
    def list_subagents() -> dict:
        """List every subagent ever deployed, newest first."""
        import friday.tools.subagents as _self

        return _self.list_subagents()

    @mcp.tool()
    def check_agent_news() -> dict:
        """
        Collect (and clear) pending finished-work notifications from
        background agents and monitors. Call at the start of a session or
        when the boss asks "anything new?" — mention each item in one
        short spoken line.
        """
        from friday.agents import runtime

        # NOTE: morning_digest (friday/tools/digest.py) consumes these same
        # flags — whichever runs first delivers the news.
        return {"status": "ok", "news": runtime.pop_pending_notifies()}
