"""
Memory search — FTS5 BM25 over the indexed vault + knowledge notes, with
the old plain-token scan as a fallback when FTS is unavailable or empty.
`Hit` and `search()` keep their shapes: friday/tools/memory.py and
friday/learning/store.py consume them unchanged.
"""

from __future__ import annotations

import logging
import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from friday.memory import vault


logger = logging.getLogger("friday.memory.search")

_IGNORED_DIRS = {".obsidian", ".trash"}
_WORD = re.compile(r"\w+")


@dataclass
class Hit:
    path: str
    title: str
    score: float
    snippet: str


def _tokenize(text: str) -> list[str]:
    return [t.lower() for t in _WORD.findall(text)]


def _fts_search(terms: list[str], k: int, namespace: str | None = None) -> list[Hit]:
    """BM25 search over notes_fts, optionally limited to one namespace prefix
    ('vault' or 'knowledge'). Raises on sqlite connect or query failure —
    caller falls back to scan."""
    from friday.memory import db

    match = " ".join(f'"{t}"*' for t in terms)
    conn = db.connect()
    try:
        if namespace is not None:
            rows = conn.execute(
                """
                SELECT path, title,
                       snippet(notes_fts, 2, '', '', '…', 24) AS snip,
                       bm25(notes_fts, 0.0, 5.0, 1.0) AS rank
                FROM notes_fts
                WHERE notes_fts MATCH ?
                  AND path LIKE ?
                ORDER BY rank
                LIMIT ?
                """,
                (match, f"{namespace}:%", k),
            ).fetchall()
        else:
            rows = conn.execute(
                """
                SELECT path, title,
                       snippet(notes_fts, 2, '', '', '…', 24) AS snip,
                       bm25(notes_fts, 0.0, 5.0, 1.0) AS rank
                FROM notes_fts
                WHERE notes_fts MATCH ?
                ORDER BY rank
                LIMIT ?
                """,
                (match, k),
            ).fetchall()
    finally:
        conn.close()
    hits = []
    for row in rows:
        raw = row["path"]
        # Strip "vault:" or "knowledge:" prefix from the path
        parts = raw.split(":", 1)
        rel = parts[1] if len(parts) > 1 else raw
        hits.append(
            Hit(
                path=rel,
                title=row["title"] or rel,
                # BM25 scores land roughly in [0.01, ~2]; the scan fallback's
                # term counts run much higher. Ledger consumers normalize.
                score=max(0.01, -float(row["rank"])),
                snippet=" ".join((row["snip"] or "").split()),
            )
        )
    return hits


# --- old plain-token scan, kept verbatim as the fallback path ----------


def _iter_notes(root: Path) -> Iterable[Path]:
    for p in root.rglob("*.md"):
        if any(part in _IGNORED_DIRS for part in p.parts):
            continue
        yield p


def _snippet(body: str, terms: list[str], width: int = 160) -> str:
    lower = body.lower()
    for term in terms:
        idx = lower.find(term)
        if idx >= 0:
            start = max(0, idx - width // 2)
            end = min(len(body), idx + width // 2)
            snip = body[start:end].replace("\n", " ").strip()
            return ("…" if start > 0 else "") + snip + ("…" if end < len(body) else "")
    return body[:width].replace("\n", " ").strip()


def _title(path: Path, body: str) -> str:
    for line in body.splitlines():
        line = line.strip()
        if line.startswith("# "):
            return line[2:].strip()
    return path.stem.replace("-", " ")


def _scan_search(terms: list[str], k: int, root: Path) -> list[Hit]:
    hits: list[Hit] = []
    for path in _iter_notes(root):
        try:
            body = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        lower = body.lower()
        title = _title(path, body)
        title_lower = title.lower()
        score = 0
        for term in terms:
            score += lower.count(term)
            score += 3 * title_lower.count(term)
        if score == 0:
            continue
        hits.append(
            Hit(
                path=str(path.relative_to(root)),
                title=title,
                score=float(score),
                snippet=_snippet(body, terms),
            )
        )
    hits.sort(key=lambda h: h.score, reverse=True)
    return hits[:k]


def search(query: str, k: int = 5, root: Path | None = None, namespace: str | None = None) -> list[Hit]:
    root = root or vault.vault_root()
    terms = _tokenize(query)
    if not terms:
        return []
    try:
        from friday.memory import index

        index.reindex_if_stale()
        hits = _fts_search(terms, k, namespace)
        if hits:
            return hits
    except (sqlite3.Error, OSError) as exc:
        logger.debug("FTS search unavailable, scanning: %s", exc)
    return _scan_search(terms, k, root)
