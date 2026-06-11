"""
LLM calls for the learning engine — research query planning + distillation.

Both calls use JSON mode on a small model (env ``FRIDAY_LEARNER_MODEL``,
default gpt-4o-mini) so background learning stays cheap. One retry on
malformed JSON, then ``LLMFailure`` — the engine treats that as a failed
round (backoff / stall, never crash).
"""

from __future__ import annotations

import json
import os

from friday.config import config


INDEX_CAP = 6_000
QUESTIONS_CAP = 2_000
PAGES_TOTAL_CAP = 40_000


class LLMFailure(RuntimeError):
    """A learner LLM call failed after retry."""


_client = None


def _get_client():
    global _client
    if _client is None:
        if not config.OPENAI_API_KEY:
            raise LLMFailure("OPENAI_API_KEY is not set — learning needs it.")
        from openai import AsyncOpenAI

        _client = AsyncOpenAI(api_key=config.OPENAI_API_KEY)
    return _client


def learner_model() -> str:
    return os.getenv("FRIDAY_LEARNER_MODEL", config.FRIDAY_LEARNER_MODEL)


async def _json_call(system: str, user: str) -> dict:
    client = _get_client()
    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]
    last_error: Exception | None = None
    for attempt in range(2):
        try:
            response = await client.chat.completions.create(
                model=learner_model(),
                messages=messages,
                temperature=0.3,
                response_format={"type": "json_object"},
            )
            return json.loads(response.choices[0].message.content or "")
        except json.JSONDecodeError as exc:
            last_error = exc
            messages.append({"role": "user", "content": "Return valid JSON only."})
        except Exception as exc:
            raise LLMFailure(f"learner LLM call failed: {exc}") from exc
    raise LLMFailure(f"learner LLM returned invalid JSON twice: {last_error}")


async def generate_queries(
    topic: str,
    index_md: str,
    open_questions_md: str,
    queries_already_run: list[str],
) -> dict:
    """Plan the next research round.

    Returns ``{"queries": [...], "coverage": "shallow|developing|good",
    "done": bool, "rationale": str}``.
    """
    system = (
        "You are a research planner for a self-learning AI assistant. "
        "Given the current notes on a topic, produce the next web search "
        "queries that fill the biggest knowledge gaps. Respond with JSON: "
        '{"queries": ["..."], "coverage": "shallow|developing|good", '
        '"done": false, "rationale": "..."}. '
        "At most 3 NEW queries, none repeating queries already run. "
        "Set done=true ONLY if the notes already answer the open questions "
        "and cover the fundamentals, current state, key debates, and "
        "practical implications of the topic."
    )
    user = (
        f"Topic: {topic}\n\n"
        f"Current notes (may be empty):\n{index_md[:INDEX_CAP]}\n\n"
        f"Open questions:\n{open_questions_md[:QUESTIONS_CAP]}\n\n"
        f"Queries already run: {json.dumps(queries_already_run[-30:])}"
    )
    result = await _json_call(system, user)
    queries = [q.strip() for q in result.get("queries", []) if isinstance(q, str) and q.strip()]
    return {
        "queries": queries[:3],
        "coverage": result.get("coverage", "shallow"),
        "done": bool(result.get("done", False)),
        "rationale": result.get("rationale", ""),
    }


def _cap_pages(pages: list[dict]) -> list[dict]:
    """Truncate page texts tail-first so the total stays under the cap."""
    capped: list[dict] = []
    budget = PAGES_TOTAL_CAP
    for page in pages:
        if budget <= 500:
            break
        text = (page.get("text") or "")[:budget]
        budget -= len(text)
        capped.append({"title": page.get("title", ""), "url": page.get("url", ""), "text": text})
    return capped


async def distill(
    topic: str,
    index_md: str,
    open_questions_md: str,
    pages: list[dict],
    existing_note_slugs: list[str],
) -> dict:
    """Merge new source pages into the topic's knowledge notes.

    Returns ``{"index_md": str, "open_questions_md": str, "coverage": str,
    "subtopic_notes": [{"slug", "title", "md"}]}``.
    """
    system = (
        "You are FRIDAY's knowledge distiller. MERGE the new source material "
        "into the existing topic notes. Keep everything that is still true, "
        "integrate new facts, cite source URLs inline as markdown links, "
        "check off or remove open questions the sources answer, and add "
        "newly raised questions as '- [ ] ...' bullets. "
        "index_md must be at most 300 lines, structured: "
        "# <Topic> / ## Summary / ## Key Concepts / ## Current State / "
        "## Notable Sources. "
        "Create subtopic_notes only for substantial subtopics (several "
        "paragraphs); reuse an existing slug when extending a note. "
        "Respond with JSON: "
        '{"index_md": "...", "open_questions_md": "...", '
        '"coverage": "shallow|developing|good", '
        '"subtopic_notes": [{"slug": "...", "title": "...", "md": "..."}]}'
    )
    payload = {
        "topic": topic,
        "existing_index_md": index_md[:INDEX_CAP],
        "existing_open_questions_md": open_questions_md[:QUESTIONS_CAP],
        "existing_subtopic_note_slugs": existing_note_slugs,
        "new_pages": _cap_pages(pages),
    }
    result = await _json_call(system, json.dumps(payload, ensure_ascii=False))
    if not isinstance(result.get("index_md"), str) or not result.get("index_md").strip():
        raise LLMFailure("distiller returned no index_md")
    notes = [
        n
        for n in result.get("subtopic_notes", [])
        if isinstance(n, dict) and isinstance(n.get("md"), str) and n.get("md").strip()
    ]
    return {
        "index_md": result["index_md"],
        "open_questions_md": result.get("open_questions_md", "") or "",
        "coverage": result.get("coverage", "developing"),
        "subtopic_notes": notes,
    }
