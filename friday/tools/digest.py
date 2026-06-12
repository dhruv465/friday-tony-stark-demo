"""
Morning digest — one tool that gathers today's events, weather, world and
finance headlines, and overnight agent news into a single raw briefing
the LLM narrates per persona. OpenJarvis morning-digest pattern, built
from Friday's own parts. Weather is keyless wttr.in JSON.

Cached per day at <knowledge>/_memory/digest-YYYY-MM-DD.md.
"""

from __future__ import annotations

import asyncio
import datetime as _dt
import logging
import os
from pathlib import Path

import httpx

from friday.config import config
from friday.tools.web import gather_finance_news, gather_world_news


logger = logging.getLogger("friday.tools.digest")

_WTTR_URL = "https://wttr.in/?format=j1"


def _cache_path(day: _dt.date) -> Path:
    root = Path(
        os.getenv("FRIDAY_KNOWLEDGE_DIR", config.FRIDAY_KNOWLEDGE_DIR)
    ).expanduser() / "_memory"
    root.mkdir(parents=True, exist_ok=True)
    return root / f"digest-{day:%Y-%m-%d}.md"


async def _weather() -> str:
    async with httpx.AsyncClient(timeout=8.0, follow_redirects=True) as client:
        resp = await client.get(_WTTR_URL, headers={"User-Agent": "curl/8"})
        resp.raise_for_status()
        data = resp.json()
    current = data["current_condition"][0]
    desc = current["weatherDesc"][0]["value"]
    temp = current["temp_C"]
    feels = current["FeelsLikeC"]
    area = ""
    try:
        nearest = data["nearest_area"][0]
        area = f" in {nearest['areaName'][0]['value']}"
    except (KeyError, IndexError):
        pass
    return f"{desc}, {temp}°C (feels like {feels}°C){area}"


def _events_section() -> str:
    try:
        from friday.memory import facts

        facts.groom_expired()
        lines = facts.itinerary_lines(cap=8)
        return "\n".join(lines) if lines else "(no tracked events)"
    except Exception as exc:
        logger.debug("digest events skipped: %s", exc)
        return "(events unavailable)"


def _agent_news_section() -> str:
    try:
        from friday.agents.runtime import pop_pending_notifies

        pending = pop_pending_notifies()
        if not pending:
            return "(nothing overnight)"
        return "\n".join(f"- {p.get('detail', '')}" for p in pending)
    except Exception as exc:
        logger.debug("digest agent news skipped: %s", exc)
        return "(agent news unavailable)"


def _top_lines(briefing: str, n: int) -> str:
    lines = [l for l in briefing.splitlines() if l.strip()][:n + 1]
    return "\n".join(lines)


async def _build() -> str:
    now = _dt.datetime.now()

    async def safe(coro, fallback: str) -> str:
        try:
            return await coro
        except Exception as exc:
            logger.debug("digest section failed: %s", exc)
            return fallback

    weather, world, finance = await asyncio.gather(
        safe(_weather(), "(weather unavailable)"),
        safe(gather_world_news(), "(world news unavailable)"),
        safe(gather_finance_news(), "(finance news unavailable)"),
    )
    return (
        f"MORNING DIGEST — {now:%A, %B %d, %Y}\n\n"
        f"## Today's events\n{_events_section()}\n\n"
        f"## Weather\n{weather}\n\n"
        f"## World\n{_top_lines(world, 5)}\n\n"
        f"## Markets\n{_top_lines(finance, 3)}\n\n"
        f"## Overnight agent news\n{_agent_news_section()}\n"
    )


def register(mcp):

    @mcp.tool()
    async def morning_digest(fresh: bool = False) -> str:
        """
        Build the boss's morning briefing: today's events, weather, top
        world + market headlines, and overnight agent news.

        Call when the boss says "good morning", "morning briefing",
        "morning digest", or asks to start the day. Cached for the day;
        fresh=true rebuilds it.

        Narrate the result as one flowing spoken briefing in persona —
        events first, then weather, then the biggest stories. Never read
        the raw sections or markdown aloud.
        """
        cache = _cache_path(_dt.date.today())
        if not fresh and cache.is_file():
            return cache.read_text(encoding="utf-8")
        text = await _build()
        try:
            cache.write_text(text, encoding="utf-8")
        except OSError as exc:
            logger.debug("digest cache write skipped: %s", exc)
        return text
