"""Self-learning MCP tools — propose / confirm broker + status + recall.

FRIDAY proposes a background learning job; the boss confirms verbally;
FRIDAY starts it. Mirrors the propose/confirm pattern used by
``friday.tools.shell`` so the agent's UX stays consistent across
permission-gated surfaces.

The actual research loop lives in ``friday.learning.engine`` (daemon
thread + private event loop); knowledge notes live in the machine-managed
``FRIDAY_KNOWLEDGE_DIR`` folder (see ``friday.learning.store``).
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass

from friday.learning import store


PENDING_TTL_SECONDS = 120
PENDING_LEARNING_ACTIONS: dict[str, "PendingLearning"] = {}

MAX_TOPIC_CHARS = 120
RECALL_INDEX_CAP = 3000


@dataclass
class PendingLearning:
    action_id: str
    topic: str
    reason: str
    created_at: float


def _prune_expired() -> None:
    now = time.time()
    expired = [
        aid for aid, act in PENDING_LEARNING_ACTIONS.items()
        if now - act.created_at > PENDING_TTL_SECONDS
    ]
    for aid in expired:
        PENDING_LEARNING_ACTIONS.pop(aid, None)


def _latest_pending_id() -> str:
    _prune_expired()
    if not PENDING_LEARNING_ACTIONS:
        raise KeyError("No pending learning proposal.")
    return max(
        PENDING_LEARNING_ACTIONS.values(),
        key=lambda a: a.created_at,
    ).action_id


def clear_pending_learning_actions() -> None:
    PENDING_LEARNING_ACTIONS.clear()


def _runtime():
    # Lazy import keeps tool registration cheap and lets tests patch the
    # runtime without pulling in httpx/openai at import time.
    from friday.learning.engine import LearningRuntime

    return LearningRuntime.instance()


def _is_active(slug: str) -> bool:
    try:
        if _runtime().is_active(slug):
            return True
    except Exception:
        pass
    return store.lock_is_live(slug)


def propose_learning(topic: str, reason: str = "") -> dict:
    """Validate + register a pending learning job. Does NOT start it."""
    topic = " ".join(topic.split()).strip()
    if not topic:
        raise ValueError("Empty topic.")
    if len(topic) > MAX_TOPIC_CHARS:
        raise ValueError(f"Topic too long — keep it under {MAX_TOPIC_CHARS} characters.")

    slug = store.slug_for(topic)
    job = store.load_job(slug)
    if job and _is_active(slug):
        return {
            "status": "already_learning",
            "topic": job["topic"],
            "slug": slug,
            "coverage": job.get("coverage"),
            "rounds_completed": job.get("rounds_completed", 0),
        }
    if job and job.get("status") == "complete":
        return {
            "status": "already_known",
            "topic": job["topic"],
            "slug": slug,
            "coverage": job.get("coverage"),
            "rounds_completed": job.get("rounds_completed", 0),
            "hint": "Use recall_knowledge to answer, or continue_learning to go deeper.",
        }

    action_id = uuid.uuid4().hex
    PENDING_LEARNING_ACTIONS[action_id] = PendingLearning(
        action_id=action_id,
        topic=topic,
        reason=reason.strip(),
        created_at=time.time(),
    )
    return {
        "status": "pending_confirmation",
        "action_id": action_id,
        "topic": topic,
        "reason": reason.strip(),
        "expires_in_seconds": PENDING_TTL_SECONDS,
    }


def confirm_learning(action_id: str | None = None) -> dict:
    """Start the most recently proposed learning job (or the one by id)."""
    if not action_id:
        action_id = _latest_pending_id()
    action = PENDING_LEARNING_ACTIONS.get(action_id)
    if action is None:
        raise KeyError(f"No pending learning proposal: {action_id}")
    if time.time() - action.created_at > PENDING_TTL_SECONDS:
        PENDING_LEARNING_ACTIONS.pop(action_id, None)
        raise TimeoutError(f"Pending learning proposal expired: {action_id}")
    PENDING_LEARNING_ACTIONS.pop(action_id, None)
    return _runtime().start_job(action.topic)


def cancel_learning(action_id: str | None = None) -> dict:
    if not action_id:
        try:
            action_id = _latest_pending_id()
        except KeyError:
            return {"status": "noop", "reason": "no pending learning proposal"}
    removed = PENDING_LEARNING_ACTIONS.pop(action_id, None)
    return {
        "status": "cancelled" if removed else "noop",
        "action_id": action_id,
    }


def learning_status(topic: str = "") -> dict:
    if not topic.strip():
        jobs = store.known_topics()
        for entry in jobs:
            entry["active_now"] = _is_active(entry["slug"])
        return {"status": "ok", "topics": jobs}
    slug = store.slug_for(topic)
    job = store.load_job(slug)
    if job is None:
        return {"status": "unknown_topic", "topic": topic}
    index_head = "\n".join(store.read_doc(slug, "index.md").splitlines()[:40])
    return {
        "status": "ok",
        "job": job,
        "active_now": _is_active(slug),
        "index_preview": index_head,
    }


def pause_learning(topic: str) -> dict:
    return _runtime().request_pause(store.slug_for(topic))


def stop_learning(topic: str) -> dict:
    return _runtime().request_stop(store.slug_for(topic))


def continue_learning(topic: str) -> dict:
    """Resume / deepen an already-authorized topic. No re-confirmation:
    the boss approved this topic when learning first started."""
    slug = store.slug_for(topic)
    job = store.load_job(slug)
    if job is None:
        return {
            "status": "unknown_topic",
            "topic": topic,
            "hint": "Use propose_learning for a brand-new topic.",
        }
    if _is_active(slug):
        return {"status": "already_active", "topic": job["topic"], "slug": slug}
    job["stalled_until"] = None
    store.save_job(slug, job)
    return _runtime().start_job(job["topic"])


def list_known_topics() -> dict:
    return {"status": "ok", "topics": store.known_topics()}


def recall_knowledge(query: str, max_results: int = 4) -> str:
    """Return learned knowledge relevant to ``query`` for context injection."""
    query = query.strip()
    if not query:
        return "No learned knowledge on that yet."
    hits = store.recall(query, k=max(1, min(max_results, 8)))
    if not hits:
        return "No learned knowledge on that yet."

    best = hits[0]
    best_slug = best.path.split("/")[0]
    lines = [f"### LEARNED KNOWLEDGE — {best.title}"]
    index_md = store.read_doc(best_slug, "index.md")
    if index_md:
        lines.append(index_md[:RECALL_INDEX_CAP])
    else:
        lines.append(best.snippet)
    related = [h for h in hits[1:] if h.path != best.path]
    if related:
        lines.append("\nRelated notes:")
        for hit in related:
            lines.append(f"- [{hit.path}] {hit.title} — {hit.snippet}")
    return "\n".join(lines)


def register(mcp):
    @mcp.tool()
    def propose_learning(topic: str, reason: str = "") -> dict:
        """
        Stage a background learning job for the boss to confirm. Use when
        the boss says "learn about X" / "study X", or when he asks about a
        topic you have no learned notes on (check recall_knowledge first).
        Returns a pending action_id; call confirm_learning after the boss
        says yes.
        """
        import friday.tools.learning as _self

        return _self.propose_learning(topic=topic, reason=reason)

    @mcp.tool()
    def confirm_learning(action_id: str | None = None) -> dict:
        """
        Start the most recently proposed learning job. Research runs in the
        background — searching the web, reading pages, and writing knowledge
        notes over multiple rounds. Only call after the boss confirms.
        """
        import friday.tools.learning as _self

        return _self.confirm_learning(action_id=action_id)

    @mcp.tool()
    def cancel_learning(action_id: str | None = None) -> dict:
        """Drop a pending learning proposal without starting it."""
        import friday.tools.learning as _self

        return _self.cancel_learning(action_id=action_id)

    @mcp.tool()
    def learning_status(topic: str = "") -> dict:
        """
        Report background learning progress. Empty topic → all topics with
        status/coverage; a specific topic → full job state + notes preview.
        """
        import friday.tools.learning as _self

        return _self.learning_status(topic=topic)

    @mcp.tool()
    def pause_learning(topic: str) -> dict:
        """Pause a running learning job after its current round."""
        import friday.tools.learning as _self

        return _self.pause_learning(topic=topic)

    @mcp.tool()
    def stop_learning(topic: str) -> dict:
        """Stop a learning job. Notes are kept; the job won't auto-resume."""
        import friday.tools.learning as _self

        return _self.stop_learning(topic=topic)

    @mcp.tool()
    def continue_learning(topic: str) -> dict:
        """
        Resume or deepen learning on an already-studied topic ("keep going
        on X", "go deeper on X"). No re-confirmation needed — the topic was
        already authorized.
        """
        import friday.tools.learning as _self

        return _self.continue_learning(topic=topic)

    @mcp.tool()
    def list_known_topics() -> dict:
        """List every topic FRIDAY has studied, with coverage and status."""
        import friday.tools.learning as _self

        return _self.list_known_topics()

    @mcp.tool()
    def recall_knowledge(query: str, max_results: int = 4) -> str:
        """
        Look up FRIDAY's learned knowledge notes. Call silently BEFORE
        answering substantive questions about a specific topic, technology,
        person, or event — FRIDAY may have studied it in the background.
        """
        import friday.tools.learning as _self

        return _self.recall_knowledge(query=query, max_results=max_results)
