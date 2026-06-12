"""
Reflection — every N voice turns, a silent small-model pass over the
recent transcript extracts stable user facts/preferences into the
profile, then runs memory housekeeping (promotion + fact grooming).
Mechanic ported from MemOS _maybe_reflect; LLM plumbing mirrors
friday/learning/llm.py (same client, same model knob).
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path

from friday.config import config


logger = logging.getLogger("friday.memory.reflect")

_client = None

_SYSTEM = (
    "You are a silent observer extracting stable user attributes from a "
    "conversation transcript. Extract ONLY deliberate, stable personal "
    "facts or preferences the USER stated about themselves. Ignore "
    "one-time emotional states, questions, and assistant statements. "
    'Respond with JSON: {"items": [{"type": "fact|preference", '
    '"key": "...", "value": "..."}]}. Empty list if nothing stable.'
)


def reflect_every() -> int:
    return int(os.getenv("FRIDAY_REFLECT_EVERY_TURNS", "12"))


def _get_client():
    global _client
    if _client is None:
        if not config.OPENAI_API_KEY:
            raise RuntimeError("OPENAI_API_KEY is not set — reflection needs it.")
        from openai import AsyncOpenAI

        _client = AsyncOpenAI(api_key=config.OPENAI_API_KEY)
    return _client


def _model() -> str:
    return os.getenv("FRIDAY_LEARNER_MODEL", config.FRIDAY_LEARNER_MODEL)


# Unlike learning/llm.py there is no malformed-JSON retry: a bad response
# just skips this reflection pass (next one is ≤12 turns away).
async def _json_call(system: str, user: str) -> dict:
    client = _get_client()
    response = await client.chat.completions.create(
        model=_model(),
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        temperature=0.2,
        response_format={"type": "json_object"},
    )
    return json.loads(response.choices[0].message.content or "{}")


async def run(transcript_lines: list[str], root: Path | None = None) -> list[tuple[str, str]]:
    """One reflection pass. Returns saved (field, value) pairs. Never raises."""
    saved: list[tuple[str, str]] = []
    try:
        from friday.memory import profile

        transcript = "\n".join(line[:200] for line in transcript_lines[-14:])
        if not transcript.strip():
            return []
        result = await _json_call(_SYSTEM, "Conversation:\n" + transcript)
        for item in result.get("items", []):
            if not isinstance(item, dict):
                continue
            key = str(item.get("key", "")).strip()
            value = str(item.get("value", "")).strip()
            if not key or not value:
                continue
            profile.update_field(key, value, root=root)
            saved.append((key, value))
        if saved:
            logger.info("reflection saved: %s", saved)
    except Exception as exc:
        logger.debug("reflection skipped: %s", exc)

    # housekeeping piggybacks on the reflection cadence
    try:
        from friday.memory import facts, recall_log

        recall_log.promote()
        facts.groom_expired()
    except Exception as exc:
        logger.debug("housekeeping skipped: %s", exc)
    return saved
