"""
Event tools — FRIDAY's internal calendar awareness. Dated events live in
the memory DB (friday/memory/facts.py), get conflict-linted, surface in
the per-turn context block, and fire proactive alerts from the runtime
scheduler. Distinct from Apple Reminders (local_apps.py): these are
FRIDAY's own awareness, no confirmation needed, fully reversible.
"""

from __future__ import annotations

import datetime as _dt

from friday.memory import facts


def _parse_iso(value: str) -> _dt.datetime | None:
    try:
        return _dt.datetime.fromisoformat(value.strip())
    except (ValueError, AttributeError):
        return None


def register(mcp):

    @mcp.tool()
    async def add_event(
        content: str,
        start: str,
        end: str = "",
        importance: float = 0.5,
    ) -> str:
        """
        Remember a dated event (meeting, flight, deadline, appointment).

        start/end: ISO datetimes like "2026-06-13T15:00". Resolve relative
        phrases ("tomorrow 3pm") yourself first — call get_current_time if
        you need today's date. end defaults to start + 1 hour.

        FRIDAY tracks it silently: it appears in your awareness, conflicts
        get flagged, and the boss gets reminded the day before and again
        at 1 hour / 15 minutes / start time.
        """
        start_dt = _parse_iso(start)
        if start_dt is None:
            return "start must be an ISO datetime like 2026-06-13T15:00."
        end_dt = _parse_iso(end) if end else None
        if end and end_dt is None:
            return "end must be an ISO datetime like 2026-06-13T16:00."
        facts.add_event(content, start_dt, end_dt, importance=importance)
        conflicts = facts.lint_conflicts()
        note = " Heads up: it overlaps another event." if conflicts else ""
        return f"Event saved: {content} at {start_dt:%a %b %d %I:%M %p}.{note}"

    @mcp.tool()
    async def cancel_event(keyword: str) -> str:
        """Cancel/forget upcoming events whose text matches the keyword."""
        removed = facts.cancel_event(keyword)
        facts.lint_conflicts()
        if removed == 0:
            return f"No upcoming event matches {keyword!r}."
        return f"Cancelled {removed} event(s) matching {keyword!r}."

    @mcp.tool()
    async def upcoming_events(days: int = 7) -> str:
        """
        List upcoming tracked events with countdown labels. Use when the
        boss asks "what's my schedule", "what's coming up", "any events".
        """
        facts.groom_expired()
        now = _dt.datetime.now()
        horizon = now + _dt.timedelta(days=max(1, min(days, 60)))
        lines = []
        for event in facts.get_active_events():
            try:
                start = _dt.datetime.fromisoformat(event["date_start"])
            except ValueError:
                continue
            if start > horizon:
                continue
            for line in facts.itinerary_lines(cap=99):
                if event["content"] in line and line not in lines:
                    lines.append(line)
                    break
        if not lines:
            return "No upcoming events tracked."
        return "UPCOMING EVENTS\n" + "\n".join(lines[:20])
