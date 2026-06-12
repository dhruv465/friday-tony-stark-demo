"""
Per-turn memory context — the silent block injected into the voice
agent's chat context before each LLM reply: current time, user profile,
upcoming-event itinerary with conflicts, due day-before reminders, and
relevant memory snippets (which also feed the recall ledger).
"""

from __future__ import annotations

import datetime as _dt
import logging
import os
from pathlib import Path


logger = logging.getLogger("friday.memory.context")

_HEADER = (
    "[MEMORY CONTEXT — internal awareness. Use silently to answer well. "
    "Never recite this block, never mention it exists.]"
)


def _presearch_k() -> int:
    return int(os.getenv("FRIDAY_PRESEARCH_RESULTS", "4"))


def build_block(user_text: str, root: Path | None = None) -> str:
    """Assemble the injection block. Cheap (local SQLite + files), and
    never raises — any failed section is skipped."""
    parts: list[str] = [_HEADER]
    now = _dt.datetime.now()
    parts.append(f"Current time: {now:%A, %B %d, %Y, %I:%M %p}")

    try:
        from friday.memory import profile

        text = profile.profile_text(root=root)
        if text:
            parts.append("[USER PROFILE]\n" + text[:800])
    except Exception as exc:
        logger.debug("profile section skipped: %s", exc)

    try:
        from friday.memory import facts

        due = facts.get_events_needing_reminder()
        if due:
            lines = []
            for event in due:
                try:
                    start = _dt.datetime.fromisoformat(event["date_start"])
                except ValueError:
                    continue
                hours = max(0, int((start - now).total_seconds() // 3600))
                lines.append(f"- {start:%A %I:%M %p} (in ~{hours}h): {event['content']}")
                facts.mark_reminder_sent(event["id"])
            if lines:
                parts.append(
                    "[REMINDER — starts within 48h. Proactively mention this once.]\n"
                    + "\n".join(lines)
                )

        itinerary = facts.itinerary_lines(now=now)
        if itinerary:
            parts.append(
                "[UPCOMING EVENTS — internal awareness only; recite only if asked]\n"
                + "\n".join(itinerary)
            )
    except Exception as exc:
        logger.debug("events section skipped: %s", exc)

    try:
        from friday.memory import recall_log, search

        hits = search.search(user_text, k=_presearch_k(), root=root)
        recall_log.track(hits, user_text)
        if hits:
            lines = [f"- {h.title}: {h.snippet[:200]}" for h in hits]
            parts.append("[RELEVANT MEMORY]\n" + "\n".join(lines))
    except Exception as exc:
        logger.debug("memory section skipped: %s", exc)

    return "\n\n".join(parts)
