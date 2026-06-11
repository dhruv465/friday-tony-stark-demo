"""
Page fetching + readability-lite text extraction for the learning engine.

Stdlib-only on purpose — no readability/trafilatura deps. Good enough for
LLM distillation: drop script/style/nav blocks, turn block tags into
newlines, strip the rest, unescape entities, collapse blank lines.
"""

from __future__ import annotations

import html as _html
import re
import urllib.robotparser
from urllib.parse import urlparse, urlunparse

import httpx


MAX_PAGE_CHARS = 20_000
MAX_BODY_BYTES = 2_000_000
FETCH_TIMEOUT_S = 15.0
ROBOTS_TIMEOUT_S = 3.0
USER_AGENT = "Friday-AI/1.0 (background research)"

_DROP_BLOCKS = re.compile(
    r"<(script|style|nav|header|footer|aside|noscript|svg|form)\b.*?</\1\s*>",
    re.DOTALL | re.IGNORECASE,
)
_COMMENTS = re.compile(r"<!--.*?-->", re.DOTALL)
_BLOCK_TAGS = re.compile(
    r"</?(p|div|br|li|ul|ol|h[1-6]|tr|td|th|table|section|article|blockquote|pre)[^>]*>",
    re.IGNORECASE,
)
_TAGS = re.compile(r"<[^>]+>")
_BLANK_LINES = re.compile(r"\n\s*\n+")


def html_to_text(html: str, max_chars: int = MAX_PAGE_CHARS) -> str:
    text = _COMMENTS.sub(" ", html)
    text = _DROP_BLOCKS.sub(" ", text)
    text = _BLOCK_TAGS.sub("\n", text)
    text = _TAGS.sub(" ", text)
    text = _html.unescape(text)
    lines = [re.sub(r"[ \t]+", " ", line).strip() for line in text.splitlines()]
    text = "\n".join(line for line in lines if line)
    text = _BLANK_LINES.sub("\n\n", text)
    return text[:max_chars]


async def fetch_page_text(
    client: httpx.AsyncClient, url: str, max_chars: int = MAX_PAGE_CHARS
) -> str:
    """Fetch a page and return extracted text. Raises on HTTP/network errors
    and on non-text or oversized responses."""
    response = await client.get(
        url,
        headers={"User-Agent": USER_AGENT, "Accept": "text/html,text/plain"},
        timeout=FETCH_TIMEOUT_S,
    )
    response.raise_for_status()
    content_type = response.headers.get("content-type", "").lower()
    if "text/html" not in content_type and "text/plain" not in content_type:
        raise ValueError(f"skipping non-text content-type: {content_type or 'unknown'}")
    if len(response.content) > MAX_BODY_BYTES:
        raise ValueError("skipping oversized page")
    if "text/plain" in content_type:
        return response.text[:max_chars]
    return html_to_text(response.text, max_chars=max_chars)


async def allowed_by_robots(
    client: httpx.AsyncClient,
    url: str,
    cache: dict[str, urllib.robotparser.RobotFileParser | None],
) -> bool:
    """Best-effort robots.txt check, one fetch per host. Any error → allow
    (politeness is the fetch delay + timeouts; robots is best-effort)."""
    parsed = urlparse(url)
    host = parsed.netloc.lower()
    if not host:
        return False
    if host not in cache:
        robots_url = urlunparse((parsed.scheme or "https", host, "/robots.txt", "", "", ""))
        parser: urllib.robotparser.RobotFileParser | None = None
        try:
            response = await client.get(
                robots_url,
                headers={"User-Agent": USER_AGENT},
                timeout=ROBOTS_TIMEOUT_S,
            )
            if response.status_code == 200:
                parser = urllib.robotparser.RobotFileParser()
                parser.parse(response.text.splitlines())
        except Exception:
            parser = None
        cache[host] = parser
    parser = cache[host]
    if parser is None:
        return True
    try:
        return parser.can_fetch(USER_AGENT, url)
    except Exception:
        return True
