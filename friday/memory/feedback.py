"""
Instant preference capture — regex patterns for unambiguous statements
("call me X", "be more concise", "no emojis"). The reflection pass
handles everything subtler. Port of the MemOS FeedbackDetector.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path

from friday.memory import profile


logger = logging.getLogger("friday.memory.feedback")

_PATTERNS: list[tuple[re.Pattern, str, int | str]] = [
    (re.compile(r"(?:call me|address me as)\s+([A-Za-z][\w]*)", re.I), "address_as", 1),
    (re.compile(r"(?:be more|be)\s+(concise|brief|short|verbose|detailed|formal|casual|friendly)\b", re.I), "response_style", 1),
    (re.compile(r"keep .*(?:short|brief|concise)", re.I), "response_style", "concise"),
    (re.compile(r"too long|too verbose|shorten", re.I), "response_style", "concise"),
    (re.compile(r"(?:be more|sound more)\s+(serious|funny|playful|warm|professional|chill)\b", re.I), "tone", 1),
    (re.compile(r"don'?t use\s+emojis?", re.I), "use_emojis", "no"),
    (re.compile(r"\buse\s+emojis?", re.I), "use_emojis", "yes"),
]

_NORMALIZE = {"brief": "concise", "short": "concise"}


def detect_and_save(user_text: str, root: Path | None = None) -> list[tuple[str, str]]:
    """Scan one user utterance; save changed preferences. Never raises."""
    saved: list[tuple[str, str]] = []
    try:
        for pattern, field, value_spec in _PATTERNS:
            match = pattern.search(user_text)
            if not match:
                continue
            value = match.group(value_spec) if isinstance(value_spec, int) else value_spec
            value = _NORMALIZE.get(value.lower(), value).strip()
            if field == "response_style":
                value = value.lower()
            if profile.get_field(field, root=root) != value:
                profile.update_field(field, value, root=root)
                saved.append((field, value))
    except Exception as exc:
        logger.debug("feedback capture skipped: %s", exc)
    return saved
