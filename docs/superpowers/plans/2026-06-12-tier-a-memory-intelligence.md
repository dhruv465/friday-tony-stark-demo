# Tier A Memory Intelligence Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Port the five Tier A features from the MemOS/OpenJarvis study into Friday: FTS5 memory search, recall tracking + promotion, temporal facts + proactive event alerts, personalization (instant capture + reflection + per-turn context injection), and a morning digest.

**Architecture:** One new SQLite database at `<FRIDAY_KNOWLEDGE_DIR>/_memory/memory.db` (WAL mode, stdlib `sqlite3`, FTS5) backs search indexing, recall tracking, and temporal facts. The markdown vault stays the source of truth for note *content* and the user profile — SQLite is an index/ledger over it. The voice agent gains a per-turn context injection hook (`on_user_turn_completed` → `ChatContext.add_message`), regex preference capture, and a every-12-turns LLM reflection. Event alerts ride the existing `SubagentRuntime` scheduler tick + `set_pending_notify`/`check_agent_news` channel. The digest is a new MCP tool composing existing news feeds + facts + weather (keyless wttr.in).

**Tech Stack:** Python ≥3.11, stdlib `sqlite3` (FTS5 verified available in `.venv`), `httpx` (already a dep), OpenAI AsyncOpenAI (already a dep, `FRIDAY_LEARNER_MODEL`), LiveKit Agents 1.x, unittest (NOT pytest — run via `uv run python -m unittest`).

**Source patterns:** MemOS (`/Users/dhruvsmac/Desktop/MemOS-Memory-Operating-System`) — `src/memory/facts.py`, `src/memory/short_term.py`, `src/memory/promotion.py`, `src/agent/loop.py` (FeedbackDetector, `_maybe_reflect`, `_pre_search`), `BACKGROUND_WORKER/proactive_events.py`. OpenJarvis — morning digest section design only. Port the *mechanics*, never copy code verbatim with its Ollama deps.

**Working directory for all commands:** `/Users/dhruvsmac/Desktop/Friday`

**Conventions that bind every task:**
- Read env at *call time* (`os.getenv(...)`) not import time, so unittest `patch.dict(os.environ, ...)` works — same pattern as `friday/security/trust.py` and `friday/learning/store.py`.
- New tool modules expose `def register(mcp):` with `@mcp.tool()` inner functions, and get wired into `friday/tools/__init__.py:register_all_tools`.
- The repo has uncommitted prior work — `git add` only the exact paths each task names, never `git add -A`.
- Voice-path code (anything imported by `agent_friday.py`) must never raise: wrap in try/except, log, continue.

---

## Phase 1 — SQLite foundation + FTS5 search

### Task 1: Memory database module

**Files:**
- Create: `friday/memory/db.py`
- Test: `tests/test_memory_db.py`

- [ ] **Step 1: Write the failing test**

```python
"""Tests for friday/memory/db.py — schema creation and env-pointed path."""

import os
import sqlite3
import tempfile
import unittest
from unittest.mock import patch


class MemoryDbTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._env = patch.dict(
            os.environ, {"FRIDAY_KNOWLEDGE_DIR": self._tmp.name}, clear=False
        )
        self._env.start()

    def tearDown(self):
        self._env.stop()
        self._tmp.cleanup()

    def test_db_path_under_knowledge_dir(self):
        from friday.memory import db

        path = db.db_path()
        self.assertTrue(str(path).startswith(self._tmp.name))
        self.assertEqual(path.name, "memory.db")

    def test_connect_creates_schema(self):
        from friday.memory import db

        conn = db.connect()
        try:
            tables = {
                row[0]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type IN ('table','view')"
                )
            }
            self.assertIn("note_meta", tables)
            self.assertIn("facts", tables)
            self.assertIn("recall_log", tables)
            # FTS5 virtual table registers as 'notes_fts'
            self.assertIn("notes_fts", tables)
            # WAL mode is on
            mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
            self.assertEqual(mode.lower(), "wal")
        finally:
            conn.close()

    def test_connect_is_idempotent(self):
        from friday.memory import db

        c1 = db.connect()
        c1.close()
        c2 = db.connect()  # second connect must not fail on existing schema
        c2.execute("INSERT INTO note_meta (path, mtime) VALUES ('x', 1.0)")
        c2.commit()
        c2.close()


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run python -m unittest tests.test_memory_db -v`
Expected: FAIL/ERROR with `ModuleNotFoundError: No module named 'friday.memory.db'`

- [ ] **Step 3: Write the implementation**

Create `friday/memory/db.py`:

```python
"""
Shared SQLite store for memory intelligence — FTS5 note index, recall
ledger, and temporal facts. The markdown vault stays the source of truth
for note content; this DB is the index/ledger over it.

Lives at <FRIDAY_KNOWLEDGE_DIR>/_memory/memory.db (same env override
pattern as the trust state and agents store, so tests can point it at a
tempdir).
"""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path

from friday.config import config


_SCHEMA = """
CREATE TABLE IF NOT EXISTS note_meta (
    path  TEXT PRIMARY KEY,
    mtime REAL NOT NULL
);

CREATE VIRTUAL TABLE IF NOT EXISTS notes_fts USING fts5(
    path UNINDEXED,
    title,
    body
);

CREATE TABLE IF NOT EXISTS facts (
    id            TEXT PRIMARY KEY,
    content       TEXT NOT NULL,
    date_start    TEXT NOT NULL,
    date_end      TEXT NOT NULL,
    importance    REAL DEFAULT 0.5,
    status        TEXT DEFAULT 'active',
    created_at    TEXT NOT NULL,
    reminder_sent TEXT,
    alerted_1h    TEXT,
    alerted_15m   TEXT,
    alerted_now   TEXT
);

CREATE TABLE IF NOT EXISTS recall_log (
    key               TEXT PRIMARY KEY,
    snippet           TEXT,
    recall_count      INTEGER DEFAULT 0,
    daily_count       INTEGER DEFAULT 0,
    total_score       REAL DEFAULT 0,
    max_score         REAL DEFAULT 0,
    first_recalled_at TEXT,
    last_recalled_at  TEXT,
    query_hashes      TEXT DEFAULT '[]',
    recall_days       TEXT DEFAULT '[]',
    concept_tags      TEXT DEFAULT '[]',
    promoted_at       TEXT
);
"""


def _knowledge_dir() -> Path:
    return Path(
        os.getenv("FRIDAY_KNOWLEDGE_DIR", config.FRIDAY_KNOWLEDGE_DIR)
    ).expanduser()


def db_path() -> Path:
    folder = _knowledge_dir() / "_memory"
    folder.mkdir(parents=True, exist_ok=True)
    return folder / "memory.db"


def connect() -> sqlite3.Connection:
    """Open a connection with WAL + schema ensured. Caller closes it."""
    conn = sqlite3.connect(db_path(), timeout=5.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.executescript(_SCHEMA)
    return conn
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run python -m unittest tests.test_memory_db -v`
Expected: PASS (3 tests)

- [ ] **Step 5: Commit**

```bash
git add friday/memory/db.py tests/test_memory_db.py
git commit -m "feat(memory): shared SQLite store with FTS5 schema for memory intelligence"
```

---

### Task 2: Note indexer (vault + knowledge → FTS)

**Files:**
- Create: `friday/memory/index.py`
- Test: `tests/test_memory_index.py`

- [ ] **Step 1: Write the failing test**

```python
"""Tests for friday/memory/index.py — incremental FTS reindexing."""

import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch


class MemoryIndexTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._env = patch.dict(
            os.environ, {"FRIDAY_KNOWLEDGE_DIR": self._tmp.name}, clear=False
        )
        self._env.start()
        self.vault_dir = Path(self._tmp.name) / "vault"
        self.knowledge_dir = Path(self._tmp.name) / "knowledge"
        self.vault_dir.mkdir()
        self.knowledge_dir.mkdir()
        (self.vault_dir / "Facts").mkdir()
        (self.vault_dir / "Facts" / "coffee.md").write_text(
            "# Coffee preference\n\nThe boss drinks black coffee, no sugar.\n",
            encoding="utf-8",
        )
        (self.knowledge_dir / "topic.md").write_text(
            "# Vector databases\n\nHNSW is an index structure.\n", encoding="utf-8"
        )
        # underscore dirs must be skipped
        hidden = self.knowledge_dir / "_agents"
        hidden.mkdir()
        (hidden / "report.md").write_text("# secret agent report\n", encoding="utf-8")

    def tearDown(self):
        self._env.stop()
        self._tmp.cleanup()

    def _reindex(self):
        from friday.memory import index

        return index.reindex(
            vault_dir=self.vault_dir, knowledge_dir=self.knowledge_dir
        )

    def _fts_paths(self):
        from friday.memory import db

        conn = db.connect()
        try:
            return {r["path"] for r in conn.execute("SELECT path FROM notes_fts")}
        finally:
            conn.close()

    def test_first_reindex_indexes_all_visible_notes(self):
        stats = self._reindex()
        self.assertEqual(stats["indexed"], 2)
        paths = self._fts_paths()
        self.assertIn("vault:Facts/coffee.md", paths)
        self.assertIn("knowledge:topic.md", paths)
        self.assertNotIn("knowledge:_agents/report.md", paths)

    def test_unchanged_files_are_skipped_on_second_pass(self):
        self._reindex()
        stats = self._reindex()
        self.assertEqual(stats["indexed"], 0)
        self.assertEqual(stats["removed"], 0)

    def test_modified_file_is_reindexed_and_deleted_file_removed(self):
        self._reindex()
        note = self.vault_dir / "Facts" / "coffee.md"
        time.sleep(0.01)
        note.write_text("# Coffee preference\n\nSwitched to espresso.\n", encoding="utf-8")
        os.utime(note, (time.time() + 5, time.time() + 5))
        (self.knowledge_dir / "topic.md").unlink()
        stats = self._reindex()
        self.assertEqual(stats["indexed"], 1)
        self.assertEqual(stats["removed"], 1)
        paths = self._fts_paths()
        self.assertNotIn("knowledge:topic.md", paths)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run python -m unittest tests.test_memory_index -v`
Expected: FAIL/ERROR with `ModuleNotFoundError: No module named 'friday.memory.index'`

- [ ] **Step 3: Write the implementation**

Create `friday/memory/index.py`:

```python
"""
Incremental indexer: walks the Obsidian vault and the knowledge dir,
mirrors every visible .md file into the notes_fts FTS5 table. mtime-diff
against note_meta keeps re-runs cheap. Underscore-prefixed folders
(_agents, _trust, _memory) and Obsidian internals are skipped.
"""

from __future__ import annotations

import logging
import os
import time
from pathlib import Path

from friday.config import config
from friday.memory import db, vault


logger = logging.getLogger("friday.memory.index")

_SKIP_PARTS_PREFIX = ("_", ".")
_STALE_INTERVAL_S = 60.0
_last_reindex_ts = 0.0


def _knowledge_dir() -> Path:
    return Path(
        os.getenv("FRIDAY_KNOWLEDGE_DIR", config.FRIDAY_KNOWLEDGE_DIR)
    ).expanduser()


def _visible(path: Path, root: Path) -> bool:
    rel = path.relative_to(root)
    return not any(part.startswith(_SKIP_PARTS_PREFIX) for part in rel.parts)


def _title_of(body: str, path: Path) -> str:
    for line in body.splitlines():
        line = line.strip()
        if line.startswith("# "):
            return line[2:].strip()
    return path.stem.replace("-", " ")


def _walk(prefix: str, root: Path) -> dict[str, Path]:
    out: dict[str, Path] = {}
    if not root.is_dir():
        return out
    for p in root.rglob("*.md"):
        if not _visible(p, root):
            continue
        out[f"{prefix}:{p.relative_to(root)}"] = p
    return out


def reindex(vault_dir: Path | None = None, knowledge_dir: Path | None = None) -> dict:
    """Sync FTS with disk. Returns {"indexed": n, "removed": n}."""
    files: dict[str, Path] = {}
    files.update(_walk("vault", vault_dir or vault.vault_root()))
    files.update(_walk("knowledge", knowledge_dir or _knowledge_dir()))

    conn = db.connect()
    indexed = removed = 0
    try:
        known = {
            r["path"]: r["mtime"] for r in conn.execute("SELECT path, mtime FROM note_meta")
        }
        for key, path in files.items():
            try:
                mtime = path.stat().st_mtime
            except OSError:
                continue
            if known.get(key) == mtime:
                continue
            try:
                body = path.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue
            conn.execute("DELETE FROM notes_fts WHERE path = ?", (key,))
            conn.execute(
                "INSERT INTO notes_fts (path, title, body) VALUES (?, ?, ?)",
                (key, _title_of(body, path), body),
            )
            conn.execute(
                "INSERT OR REPLACE INTO note_meta (path, mtime) VALUES (?, ?)",
                (key, mtime),
            )
            indexed += 1
        for key in set(known) - set(files):
            conn.execute("DELETE FROM notes_fts WHERE path = ?", (key,))
            conn.execute("DELETE FROM note_meta WHERE path = ?", (key,))
            removed += 1
        conn.commit()
    finally:
        conn.close()
    if indexed or removed:
        logger.info("memory index: +%d / -%d notes", indexed, removed)
    return {"indexed": indexed, "removed": removed}


def reindex_if_stale() -> None:
    """Throttled reindex — at most once per minute, never raises."""
    global _last_reindex_ts
    now = time.time()
    if now - _last_reindex_ts < _STALE_INTERVAL_S:
        return
    _last_reindex_ts = now
    try:
        reindex()
    except Exception as exc:
        logger.debug("reindex skipped: %s", exc)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run python -m unittest tests.test_memory_index -v`
Expected: PASS (3 tests)

- [ ] **Step 5: Commit**

```bash
git add friday/memory/index.py tests/test_memory_index.py
git commit -m "feat(memory): incremental FTS5 indexer over vault + knowledge notes"
```

---

### Task 3: FTS5-backed search with token-scan fallback

**Files:**
- Modify: `friday/memory/search.py` (rewrite — keep `Hit` shape and `search()` signature so `friday/tools/memory.py:81` and `friday/learning/store.py:256` keep working)
- Test: `tests/test_memory_search.py`

- [ ] **Step 1: Write the failing test**

```python
"""Tests for FTS5-backed friday/memory/search.py with scan fallback."""

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


class MemorySearchTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._env = patch.dict(
            os.environ, {"FRIDAY_KNOWLEDGE_DIR": self._tmp.name}, clear=False
        )
        self._env.start()
        self.vault_dir = Path(self._tmp.name) / "vault"
        (self.vault_dir / "Facts").mkdir(parents=True)
        (self.vault_dir / "Facts" / "coffee.md").write_text(
            "# Coffee preference\n\nThe boss drinks black coffee, no sugar, every morning.\n",
            encoding="utf-8",
        )
        (self.vault_dir / "Facts" / "tea.md").write_text(
            "# Tea\n\nGreen tea only when sick.\n", encoding="utf-8"
        )
        from friday.memory import index

        index.reindex(vault_dir=self.vault_dir, knowledge_dir=Path(self._tmp.name) / "nokn")

    def tearDown(self):
        self._env.stop()
        self._tmp.cleanup()

    def test_fts_search_finds_note_and_fills_hit_fields(self):
        from friday.memory import search

        hits = search.search("black coffee", k=5, root=self.vault_dir)
        self.assertTrue(hits)
        top = hits[0]
        self.assertEqual(top.path, "Facts/coffee.md")
        self.assertEqual(top.title, "Coffee preference")
        self.assertIn("coffee", top.snippet.lower())
        self.assertGreater(top.score, 0)

    def test_no_match_returns_empty(self):
        from friday.memory import search

        self.assertEqual(search.search("quantum chromodynamics", root=self.vault_dir), [])

    def test_scan_fallback_used_when_fts_unavailable(self):
        from friday.memory import search

        with patch.object(search, "_fts_search", side_effect=Exception("no fts")):
            hits = search.search("green tea", k=5, root=self.vault_dir)
        self.assertTrue(hits)
        self.assertEqual(hits[0].path, "Facts/tea.md")

    def test_weird_query_chars_do_not_crash(self):
        from friday.memory import search

        hits = search.search('coffee" OR 1=1 -- (*)', k=3, root=self.vault_dir)
        self.assertIsInstance(hits, list)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run python -m unittest tests.test_memory_search -v`
Expected: FAIL — `AttributeError: ... no attribute '_fts_search'` and/or path assertions fail (old scorer returns vault-relative path only by accident of root arg; `_fts_search` doesn't exist yet)

- [ ] **Step 3: Rewrite `friday/memory/search.py`**

Replace the entire file with:

```python
"""
Memory search — FTS5 BM25 over the indexed vault + knowledge notes, with
the old plain-token scan as a fallback when FTS is unavailable or empty.
`Hit` and `search()` keep their shapes: friday/tools/memory.py and
friday/learning/store.py consume them unchanged.
"""

from __future__ import annotations

import logging
import re
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


def _fts_search(terms: list[str], k: int) -> list[Hit]:
    """BM25 search over notes_fts. Raises on any sqlite trouble — caller
    falls back to the scan."""
    from friday.memory import db

    match = " ".join(f'"{t}"*' for t in terms)
    conn = db.connect()
    try:
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
        rel = raw.split(":", 1)[1] if ":" in raw else raw
        hits.append(
            Hit(
                path=rel,
                title=row["title"] or rel,
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


def search(query: str, k: int = 5, root: Path | None = None) -> list[Hit]:
    root = root or vault.vault_root()
    terms = _tokenize(query)
    if not terms:
        return []
    try:
        from friday.memory import index

        index.reindex_if_stale()
        hits = _fts_search(terms, k)
        if hits:
            return hits
    except Exception as exc:
        logger.debug("FTS search unavailable, scanning: %s", exc)
    return _scan_search(terms, k, root)
```

Note: when FTS returns zero hits the scan fallback still runs — FTS misses nothing the scan would find (scan is substring-based, FTS is prefix-token-based), so this keeps recall at least as good as before.

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run python -m unittest tests.test_memory_search -v`
Expected: PASS (4 tests)

- [ ] **Step 5: Run the full suite to catch regressions in consumers**

Run: `uv run python -m unittest discover -s tests`
Expected: all green (learning recall + memory tool tests still pass against the new `search()`)

- [ ] **Step 6: Commit**

```bash
git add friday/memory/search.py tests/test_memory_search.py
git commit -m "feat(memory): FTS5 BM25 search with plain-scan fallback"
```

---

## Phase 2 — Recall tracking + promotion

### Task 4: Recall ledger and promotion scoring

**Files:**
- Create: `friday/memory/recall_log.py`
- Test: `tests/test_recall_promotion.py`

Port of MemOS `short_term.py` + `promotion.py` (scoring weights identical: frequency .20, relevance .25, diversity .15, recency .10, consolidation .20, conceptual .10; threshold 0.55). Promoted snippets land in the vault at `Profile/promoted.md`.

- [ ] **Step 1: Write the failing test**

```python
"""Tests for friday/memory/recall_log.py — tracking + promotion."""

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


class RecallPromotionTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._env = patch.dict(
            os.environ, {"FRIDAY_KNOWLEDGE_DIR": self._tmp.name}, clear=False
        )
        self._env.start()

    def tearDown(self):
        self._env.stop()
        self._tmp.cleanup()

    def _hit(self, path="Facts/coffee.md", score=2.0):
        from friday.memory.search import Hit

        return Hit(path=path, title="Coffee", score=score, snippet="boss drinks black coffee")

    def test_track_inserts_then_increments(self):
        from friday.memory import db, recall_log

        recall_log.track([self._hit()], "what coffee does he like")
        recall_log.track([self._hit()], "morning drink preference")
        conn = db.connect()
        try:
            row = conn.execute("SELECT * FROM recall_log").fetchone()
        finally:
            conn.close()
        self.assertEqual(row["recall_count"], 2)
        import json

        self.assertEqual(len(json.loads(row["query_hashes"])), 2)

    def test_evaluate_scores_in_unit_range_and_needs_recalls(self):
        from friday.memory.recall_log import evaluate_candidate

        empty = evaluate_candidate(
            {"recall_count": 0, "total_score": 0, "query_hashes": [], "recall_days": [], "concept_tags": []}
        )
        self.assertFalse(empty["valid"])
        strong = evaluate_candidate(
            {
                "recall_count": 8,
                "total_score": 8.0,
                "query_hashes": ["a", "b", "c", "d", "e"],
                "recall_days": ["2026-06-01", "2026-06-03", "2026-06-05"],
                "concept_tags": ["coffee", "morning", "preference"],
            }
        )
        self.assertTrue(strong["valid"])
        self.assertGreaterEqual(strong["score"], 0.55)
        self.assertLessEqual(strong["score"], 1.0)

    def test_promote_writes_vault_note_and_marks_row(self):
        from friday.memory import db, recall_log

        vault_dir = Path(self._tmp.name) / "vault"
        for i in range(8):
            recall_log.track([self._hit()], f"distinct query number {i}")
        # spread recall_days artificially so consolidation scores
        conn = db.connect()
        conn.execute(
            "UPDATE recall_log SET recall_days = ?",
            ('["2026-06-01","2026-06-03","2026-06-05"]',),
        )
        conn.commit()
        conn.close()
        promoted = recall_log.promote(vault_dir=vault_dir)
        self.assertEqual(len(promoted), 1)
        note = vault_dir / "Profile" / "promoted.md"
        self.assertTrue(note.exists())
        self.assertIn("black coffee", note.read_text(encoding="utf-8"))
        # second promote run: nothing left
        self.assertEqual(recall_log.promote(vault_dir=vault_dir), [])


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run python -m unittest tests.test_recall_promotion -v`
Expected: FAIL/ERROR — `No module named 'friday.memory.recall_log'`

- [ ] **Step 3: Write the implementation**

Create `friday/memory/recall_log.py`:

```python
"""
Recall ledger + promotion — every search hit is tracked; memories that
keep getting recalled across days and distinct queries "crystallize" into
Profile/promoted.md. Port of the MemOS short-term-recall/promotion
mechanic (same scoring weights, threshold 0.55).
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import json
import logging
import os
import re
from pathlib import Path

from friday.memory import db


logger = logging.getLogger("friday.memory.recall")

_STOP_WORDS = {
    "a", "an", "and", "are", "as", "at", "be", "for", "from", "in", "is",
    "it", "of", "on", "or", "that", "the", "this", "to", "with",
}
_MAX_ENTRIES = 500


def _promote_threshold() -> float:
    return float(os.getenv("FRIDAY_PROMOTE_THRESHOLD", "0.55"))


def _hash_query(query: str) -> str:
    normalized = " ".join(query.lower().split())
    return hashlib.sha1(normalized.encode()).hexdigest()[:12]


def _concept_tags(path: str, text: str) -> list[str]:
    words = [Path(path).stem.lower()]
    words.extend(re.findall(r"[a-zA-Z][a-zA-Z0-9_+-]{2,}", text.lower()))
    seen: list[str] = []
    for word in words:
        if word not in _STOP_WORDS and word not in seen:
            seen.append(word)
    return seen[:12]


def track(hits, query: str) -> None:
    """Record one recall event per hit. Never raises."""
    if not hits:
        return
    try:
        _track(hits, query)
    except Exception as exc:
        logger.debug("recall tracking skipped: %s", exc)


def _track(hits, query: str) -> None:
    now_iso = _dt.datetime.now(_dt.timezone.utc).isoformat()
    today = now_iso[:10]
    qhash = _hash_query(query)
    conn = db.connect()
    try:
        for hit in hits:
            key = hit.path
            score = float(hit.score or 0.0)
            row = conn.execute("SELECT * FROM recall_log WHERE key = ?", (key,)).fetchone()
            if row is None:
                conn.execute(
                    """INSERT INTO recall_log
                    (key, snippet, recall_count, daily_count, total_score, max_score,
                     first_recalled_at, last_recalled_at, query_hashes, recall_days,
                     concept_tags)
                    VALUES (?, ?, 1, 1, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        key, (hit.snippet or "")[:300], score, score, now_iso, now_iso,
                        json.dumps([qhash]), json.dumps([today]),
                        json.dumps(_concept_tags(key, hit.snippet or "")),
                    ),
                )
                continue
            query_hashes = json.loads(row["query_hashes"] or "[]")
            recall_days = json.loads(row["recall_days"] or "[]")
            if qhash not in query_hashes:
                query_hashes.append(qhash)
            daily_count = row["daily_count"]
            if today not in recall_days:
                recall_days.append(today)
                daily_count += 1
            conn.execute(
                """UPDATE recall_log SET recall_count = recall_count + 1,
                daily_count = ?, total_score = total_score + ?,
                max_score = MAX(max_score, ?), last_recalled_at = ?,
                query_hashes = ?, recall_days = ? WHERE key = ?""",
                (daily_count, score, score, now_iso,
                 json.dumps(query_hashes), json.dumps(recall_days), key),
            )
        conn.commit()
    finally:
        conn.close()


def _clamp(value: float) -> float:
    return max(0.0, min(1.0, value))


def evaluate_candidate(entry: dict) -> dict:
    """MemOS promotion scoring — weights .20/.25/.15/.10/.20/.10."""
    recall_count = int(entry.get("recall_count", 0) or 0)
    total_score = float(entry.get("total_score", 0) or 0)
    query_hashes = entry.get("query_hashes") or []
    recall_days = entry.get("recall_days") or []
    concept_tags = entry.get("concept_tags") or []

    unique_days = sorted(set(recall_days))
    consolidation = _clamp(0.2 + (len(unique_days) - 1) * 0.15) if unique_days else 0.0
    signal_count = max(1, recall_count)
    components = {
        "frequency": _clamp(recall_count / 8.0),
        "relevance": _clamp(total_score / signal_count),
        "diversity": _clamp(len(set(query_hashes)) / 5.0),
        "recency": 1.0,
        "consolidation": consolidation,
        "conceptual": _clamp(len(set(concept_tags)) / 6.0),
    }
    score = (
        components["frequency"] * 0.2
        + components["relevance"] * 0.25
        + components["diversity"] * 0.15
        + components["recency"] * 0.1
        + components["consolidation"] * 0.2
        + components["conceptual"] * 0.1
    )
    return {"valid": recall_count > 0, "score": _clamp(score), "components": components}


def promote(vault_dir: Path | None = None) -> list[dict]:
    """Promote strong unpromoted recalls into Profile/promoted.md."""
    from friday.memory import vault

    conn = db.connect()
    promoted: list[dict] = []
    now_iso = _dt.datetime.now(_dt.timezone.utc).isoformat()
    try:
        rows = conn.execute(
            "SELECT * FROM recall_log WHERE promoted_at IS NULL "
            "ORDER BY max_score DESC LIMIT 20"
        ).fetchall()
        for row in rows:
            entry = {
                "recall_count": row["recall_count"],
                "total_score": row["total_score"],
                "query_hashes": json.loads(row["query_hashes"] or "[]"),
                "recall_days": json.loads(row["recall_days"] or "[]"),
                "concept_tags": json.loads(row["concept_tags"] or "[]"),
            }
            result = evaluate_candidate(entry)
            if result["valid"] and result["score"] >= _promote_threshold():
                conn.execute(
                    "UPDATE recall_log SET promoted_at = ? WHERE key = ?",
                    (now_iso, row["key"]),
                )
                promoted.append(
                    {"key": row["key"], "snippet": row["snippet"] or "", "score": result["score"]}
                )
        # prune the long tail so the ledger stays bounded
        conn.execute(
            "DELETE FROM recall_log WHERE key IN ("
            "SELECT key FROM recall_log ORDER BY last_recalled_at DESC "
            "LIMIT -1 OFFSET ?)",
            (_MAX_ENTRIES,),
        )
        conn.commit()
    finally:
        conn.close()

    if promoted:
        root = vault_dir or vault.vault_root()
        note = root / "Profile" / "promoted.md"
        note.parent.mkdir(parents=True, exist_ok=True)
        with note.open("a", encoding="utf-8") as f:
            if note.stat().st_size == 0:
                f.write("# Promoted memories\n\nRecall-crystallized knowledge.\n\n")
            for item in promoted:
                f.write(f"- {item['snippet']} (from {item['key']}, score {item['score']:.2f})\n")
        logger.info("promoted %d memories", len(promoted))
    return promoted
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run python -m unittest tests.test_recall_promotion -v`
Expected: PASS (3 tests)

- [ ] **Step 5: Commit**

```bash
git add friday/memory/recall_log.py tests/test_recall_promotion.py
git commit -m "feat(memory): recall ledger + MemOS-style promotion to Profile/promoted.md"
```

---

### Task 5: Wire tracking into the search tool + server boot reindex

**Files:**
- Modify: `friday/tools/memory.py` (the `search_memory` tool)
- Modify: `server.py` (`_lifespan`)

- [ ] **Step 1: Hook tracking into `search_memory`**

In `friday/tools/memory.py`, change the `search_memory` body (currently lines 74–87) from:

```python
    @mcp.tool()
    async def search_memory(query: str, max_results: int = 5) -> str:
        """
        Keyword search across every note in the vault. Returns the most
        relevant notes with a short snippet. Use this before answering
        questions where memory might help — "what did I tell you about X",
        "who is Y", "what's my Z".
        """
        hits = search.search(query, k=max(1, min(max_results, 20)))
        if not hits:
            return f"No notes match {query!r}."
```

to:

```python
    @mcp.tool()
    async def search_memory(query: str, max_results: int = 5) -> str:
        """
        Keyword search across every note in the vault. Returns the most
        relevant notes with a short snippet. Use this before answering
        questions where memory might help — "what did I tell you about X",
        "who is Y", "what's my Z".
        """
        hits = search.search(query, k=max(1, min(max_results, 20)))
        recall_log.track(hits, query)
        if not hits:
            return f"No notes match {query!r}."
```

and extend the import at the top of the file from:

```python
from friday.memory import vault, journal, search
```

to:

```python
from friday.memory import vault, journal, recall_log, search
```

- [ ] **Step 2: Reindex on server boot**

In `server.py`, inside `_lifespan` after the learning-resume block and before `yield {}`, add:

```python
    # Build/refresh the memory FTS index so first searches are warm.
    try:
        from friday.memory.index import reindex

        stats = reindex()
        logging.getLogger("friday").info("Memory index ready: %s", stats)
    except Exception as exc:
        logging.getLogger("friday").warning("Memory reindex skipped: %s", exc)
```

- [ ] **Step 3: Run the full suite**

Run: `uv run python -m unittest discover -s tests`
Expected: all green

- [ ] **Step 4: Commit**

```bash
git add friday/tools/memory.py server.py
git commit -m "feat(memory): track recalls from search_memory; reindex FTS at server boot"
```

---

## Phase 3 — Temporal facts + proactive alerts

### Task 6: Facts store (events with conflict linting and alert windows)

**Files:**
- Create: `friday/memory/facts.py`
- Test: `tests/test_facts_store.py`

Port of MemOS `facts.py` + the alert-window logic from `proactive_events.py`, with alert dedup moved from a JSON state file into the `alerted_1h/15m/now` columns.

- [ ] **Step 1: Write the failing test**

```python
"""Tests for friday/memory/facts.py — temporal events, conflicts, alerts."""

import datetime as dt
import os
import tempfile
import unittest
from unittest.mock import patch


class FactsStoreTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._env = patch.dict(
            os.environ, {"FRIDAY_KNOWLEDGE_DIR": self._tmp.name}, clear=False
        )
        self._env.start()

    def tearDown(self):
        self._env.stop()
        self._tmp.cleanup()

    def _add(self, content, start, hours=1.0, importance=0.5):
        from friday.memory import facts

        return facts.add_event(
            content, start, start + dt.timedelta(hours=hours), importance=importance
        )

    def test_add_and_list_active_sorted(self):
        from friday.memory import facts

        now = dt.datetime.now()
        self._add("dentist", now + dt.timedelta(days=2))
        self._add("standup", now + dt.timedelta(days=1))
        active = facts.get_active_events()
        self.assertEqual([f["content"] for f in active], ["standup", "dentist"])

    def test_cancel_by_keyword(self):
        from friday.memory import facts

        now = dt.datetime.now()
        self._add("dentist appointment", now + dt.timedelta(days=2))
        removed = facts.cancel_event("dentist")
        self.assertEqual(removed, 1)
        self.assertEqual(facts.get_active_events(), [])

    def test_overlap_marks_both_contested(self):
        from friday.memory import facts

        start = dt.datetime.now() + dt.timedelta(days=1)
        self._add("meeting A", start, hours=2)
        self._add("meeting B", start + dt.timedelta(minutes=30), hours=2)
        facts.lint_conflicts()
        contested = facts.get_contested_events()
        self.assertEqual(len(contested), 2)

    def test_day_before_reminder_window_fires_once(self):
        from friday.memory import facts

        start = dt.datetime.now() + dt.timedelta(hours=30)
        self._add("flight to Delhi", start)
        due = facts.get_events_needing_reminder()
        self.assertEqual(len(due), 1)
        facts.mark_reminder_sent(due[0]["id"])
        self.assertEqual(facts.get_events_needing_reminder(), [])

    def test_alert_windows_fire_once_each(self):
        from friday.memory import facts

        now = dt.datetime.now()
        self._add("call with investor", now + dt.timedelta(minutes=55))
        self._add("gym", now + dt.timedelta(minutes=15))
        self._add("launch", now + dt.timedelta(minutes=1))
        due = facts.events_due_for_alert(now=now)
        windows = {(d["content"], d["window"]) for d in due}
        self.assertEqual(
            windows,
            {("call with investor", "1h"), ("gym", "15m"), ("launch", "now")},
        )
        for d in due:
            facts.mark_alerted(d["id"], d["window"])
        self.assertEqual(facts.events_due_for_alert(now=now), [])

    def test_groom_expires_past_events(self):
        from friday.memory import facts

        past = dt.datetime.now() - dt.timedelta(days=2)
        self._add("yesterday thing", past)
        expired = facts.groom_expired()
        self.assertEqual(expired, 1)
        self.assertEqual(facts.get_active_events(), [])


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run python -m unittest tests.test_facts_store -v`
Expected: FAIL/ERROR — `No module named 'friday.memory.facts'`

- [ ] **Step 3: Write the implementation**

Create `friday/memory/facts.py`:

```python
"""
Temporal facts — dated events with conflict linting, day-before
reminders, and minute-level alert windows. Port of the MemOS FactStore +
ProactiveEventsWatcher mechanics onto Friday's shared memory.db. Alert
dedup lives in columns (alerted_1h/15m/now), not a side file.
"""

from __future__ import annotations

import datetime as _dt
import uuid

from friday.memory import db


def _now_iso() -> str:
    return _dt.datetime.now().isoformat(timespec="seconds")


def _as_iso(value: _dt.datetime) -> str:
    return value.replace(microsecond=0).isoformat()


def _parse(value: str) -> _dt.datetime:
    return _dt.datetime.fromisoformat(value).replace(tzinfo=None)


def add_event(
    content: str,
    date_start: _dt.datetime,
    date_end: _dt.datetime | None = None,
    importance: float = 0.5,
) -> str:
    date_end = date_end or (date_start + _dt.timedelta(hours=1))
    fact_id = str(uuid.uuid4())
    conn = db.connect()
    try:
        conn.execute(
            """INSERT INTO facts
            (id, content, date_start, date_end, importance, status, created_at)
            VALUES (?, ?, ?, ?, ?, 'active', ?)""",
            (fact_id, content.strip(), _as_iso(date_start), _as_iso(date_end),
             importance, _now_iso()),
        )
        conn.commit()
    finally:
        conn.close()
    return fact_id


def cancel_event(keyword: str) -> int:
    conn = db.connect()
    try:
        cur = conn.execute(
            "UPDATE facts SET status = 'deleted' WHERE status IN ('active','contested') "
            "AND lower(content) LIKE ?",
            (f"%{keyword.lower()}%",),
        )
        conn.commit()
        return cur.rowcount
    finally:
        conn.close()


def _rows(where: str, params: tuple = ()) -> list[dict]:
    conn = db.connect()
    try:
        rows = conn.execute(
            f"SELECT * FROM facts WHERE {where} ORDER BY date_start ASC", params
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def get_active_events() -> list[dict]:
    return _rows("status IN ('active','contested')")


def get_contested_events() -> list[dict]:
    return _rows("status = 'contested'")


def _overlaps(left: dict, right: dict) -> bool:
    try:
        ls, le = _parse(left["date_start"]), _parse(left["date_end"])
        rs, re_ = _parse(right["date_start"]), _parse(right["date_end"])
    except Exception:
        return False
    return ls <= re_ and rs <= le


def lint_conflicts() -> int:
    """Re-derive contested status from scratch on every run."""
    conn = db.connect()
    try:
        rows = [dict(r) for r in conn.execute(
            "SELECT * FROM facts WHERE status IN ('active','contested')"
        ).fetchall()]
        contested: set[str] = set()
        for i, left in enumerate(rows):
            for right in rows[i + 1:]:
                if _overlaps(left, right):
                    contested.add(left["id"])
                    contested.add(right["id"])
        conn.execute("UPDATE facts SET status = 'active' WHERE status = 'contested'")
        for fact_id in contested:
            conn.execute("UPDATE facts SET status = 'contested' WHERE id = ?", (fact_id,))
        conn.commit()
        return len(contested)
    finally:
        conn.close()


def get_events_needing_reminder() -> list[dict]:
    """Events starting 20–48h from now that haven't had their day-before
    reminder yet."""
    now = _dt.datetime.now()
    start = now + _dt.timedelta(hours=20)
    end = now + _dt.timedelta(hours=48)
    return _rows(
        "status IN ('active','contested') AND reminder_sent IS NULL "
        "AND date_start BETWEEN ? AND ?",
        (_as_iso(start), _as_iso(end)),
    )


def mark_reminder_sent(fact_id: str) -> None:
    conn = db.connect()
    try:
        conn.execute("UPDATE facts SET reminder_sent = ? WHERE id = ?", (_now_iso(), fact_id))
        conn.commit()
    finally:
        conn.close()


_WINDOWS = (
    # (window key, column, min minutes-until, max minutes-until)
    ("1h", "alerted_1h", 45.0, 65.0),
    ("15m", "alerted_15m", 10.0, 20.0),
    ("now", "alerted_now", -2.0, 2.0),
)


def events_due_for_alert(now: _dt.datetime | None = None) -> list[dict]:
    """All (event, window) pairs whose alert window is open and unfired.
    Each dict gains 'window' and 'minutes_until' keys."""
    now = now or _dt.datetime.now()
    due: list[dict] = []
    for row in get_active_events():
        try:
            start = _parse(row["date_start"])
        except Exception:
            continue
        mins = (start - now).total_seconds() / 60.0
        for window, column, lo, hi in _WINDOWS:
            if lo <= mins <= hi and not row.get(column):
                entry = dict(row)
                entry["window"] = window
                entry["minutes_until"] = mins
                due.append(entry)
                break  # one window per event per pass
    return due


def mark_alerted(fact_id: str, window: str) -> None:
    column = {"1h": "alerted_1h", "15m": "alerted_15m", "now": "alerted_now"}[window]
    conn = db.connect()
    try:
        conn.execute(f"UPDATE facts SET {column} = ? WHERE id = ?", (_now_iso(), fact_id))
        conn.commit()
    finally:
        conn.close()


def groom_expired() -> int:
    conn = db.connect()
    try:
        cur = conn.execute(
            "UPDATE facts SET status = 'expired' "
            "WHERE status IN ('active','contested') AND date_end < ?",
            (_now_iso(),),
        )
        conn.commit()
        return cur.rowcount
    finally:
        conn.close()


def itinerary_lines(now: _dt.datetime | None = None, cap: int = 12) -> list[str]:
    """Countdown-labelled upcoming events for prompt injection."""
    now = now or _dt.datetime.now()
    lines: list[str] = []
    for f in get_active_events():
        try:
            ds = _parse(f["date_start"])
            de = _parse(f["date_end"])
        except Exception:
            continue
        days_until = (ds.date() - now.date()).days
        if de < now:
            continue
        if days_until <= 0:
            label = f"TODAY {ds.strftime('%I:%M %p')}–{de.strftime('%I:%M %p')}"
        elif days_until == 1:
            label = f"TOMORROW {ds.strftime('%I:%M %p')}"
        else:
            label = f"{ds.strftime('%a %b %d')} ({days_until} days away)"
        suffix = " [CONFLICTS with another event]" if f["status"] == "contested" else ""
        lines.append(f"- {label}: {f['content']}{suffix}")
        if len(lines) >= cap:
            break
    return lines
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run python -m unittest tests.test_facts_store -v`
Expected: PASS (6 tests)

- [ ] **Step 5: Commit**

```bash
git add friday/memory/facts.py tests/test_facts_store.py
git commit -m "feat(memory): temporal facts store with conflict linting and alert windows"
```

---

### Task 7: Event MCP tools

**Files:**
- Create: `friday/tools/events.py`
- Modify: `friday/tools/__init__.py`
- Test: `tests/test_event_tools.py`

- [ ] **Step 1: Write the failing test**

The existing tool tests exercise broker functions through module-level helpers; here the tools are thin, so test through a fake MCP that captures the registered functions (same pattern as other tool tests in this repo — check `tests/test_hud_tools.py` for the local `FakeMCP` and mirror it):

```python
"""Tests for friday/tools/events.py MCP tools."""

import asyncio
import datetime as dt
import os
import tempfile
import unittest
from unittest.mock import patch


class FakeMCP:
    def __init__(self):
        self.tools = {}

    def tool(self):
        def decorator(fn):
            self.tools[fn.__name__] = fn
            return fn

        return decorator


class EventToolsTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._env = patch.dict(
            os.environ, {"FRIDAY_KNOWLEDGE_DIR": self._tmp.name}, clear=False
        )
        self._env.start()
        from friday.tools import events

        self.mcp = FakeMCP()
        events.register(self.mcp)

    def tearDown(self):
        self._env.stop()
        self._tmp.cleanup()

    def _run(self, name, **kwargs):
        return asyncio.run(self.mcp.tools[name](**kwargs))

    def test_add_event_with_iso_start(self):
        start = (dt.datetime.now() + dt.timedelta(days=1)).isoformat(timespec="minutes")
        out = self._run("add_event", content="dentist", start=start)
        self.assertIn("dentist", out)
        listing = self._run("upcoming_events")
        self.assertIn("dentist", listing)
        self.assertIn("TOMORROW", listing)

    def test_add_event_rejects_bad_datetime(self):
        out = self._run("add_event", content="x", start="next tuesday-ish")
        self.assertIn("ISO", out)

    def test_cancel_event(self):
        start = (dt.datetime.now() + dt.timedelta(days=1)).isoformat(timespec="minutes")
        self._run("add_event", content="dentist", start=start)
        out = self._run("cancel_event", keyword="dentist")
        self.assertIn("1", out)
        self.assertIn("no upcoming events", self._run("upcoming_events").lower())


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run python -m unittest tests.test_event_tools -v`
Expected: FAIL/ERROR — `No module named 'friday.tools.events'`

- [ ] **Step 3: Write the implementation**

Create `friday/tools/events.py`:

```python
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
```

- [ ] **Step 4: Register the module**

In `friday/tools/__init__.py`, add `events` to the import tuple (alphabetical slot after `diagnostics`):

```python
from friday.tools import (
    desktop,
    diagnostics,
    events,
    hud,
    ...
)
```

and in `register_all_tools(mcp)` add (after `memory.register(mcp)`):

```python
    events.register(mcp)
```

- [ ] **Step 5: Run test to verify it passes**

Run: `uv run python -m unittest tests.test_event_tools -v`
Expected: PASS (3 tests)

- [ ] **Step 6: Commit**

```bash
git add friday/tools/events.py friday/tools/__init__.py tests/test_event_tools.py
git commit -m "feat(tools): add_event/cancel_event/upcoming_events backed by facts store"
```

---

### Task 8: Proactive alert watcher in the runtime scheduler

**Files:**
- Modify: `friday/agents/runtime.py` (scheduler loop ~line 1006, plus a new module function near `_notify` ~line 908; new `ensure_running()` method on `SubagentRuntime`)
- Modify: `server.py` (`_lifespan` — start the runtime so alerts fire without any agent deployed)
- Test: `tests/test_event_alerts.py`

- [ ] **Step 1: Write the failing test**

```python
"""Tests for the event-alert sweep in friday/agents/runtime.py."""

import datetime as dt
import os
import tempfile
import unittest
from unittest.mock import patch


class EventAlertTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._env = patch.dict(
            os.environ,
            {
                "FRIDAY_KNOWLEDGE_DIR": self._tmp.name,
                "FRIDAY_DESKTOP_EVENT_LOG": os.path.join(self._tmp.name, "events.jsonl"),
            },
            clear=False,
        )
        self._env.start()

    def tearDown(self):
        self._env.stop()
        self._tmp.cleanup()

    def test_due_event_notifies_once(self):
        from friday.agents import runtime
        from friday.memory import facts

        start = dt.datetime.now() + dt.timedelta(minutes=15)
        facts.add_event("investor call", start)

        with patch.object(runtime, "_mac_notification") as mac, patch.object(
            runtime, "set_pending_notify"
        ) as pending:
            runtime.check_event_alerts()
            self.assertEqual(mac.call_count, 1)
            self.assertEqual(pending.call_count, 1)
            detail = pending.call_args[0][1]
            self.assertIn("investor call", detail)
            self.assertIn("15", detail)
            # second sweep: window already marked, nothing fires
            runtime.check_event_alerts()
            self.assertEqual(mac.call_count, 1)

    def test_alert_phrasing_per_window(self):
        from friday.agents import runtime

        self.assertIn("starting now", runtime._alert_line({"content": "x", "window": "now", "minutes_until": 0.0}))
        self.assertIn("in about an hour", runtime._alert_line({"content": "x", "window": "1h", "minutes_until": 55.0}))
        self.assertIn("in 12 minutes", runtime._alert_line({"content": "x", "window": "15m", "minutes_until": 12.4}))

    def test_sweep_never_raises(self):
        from friday.agents import runtime

        with patch("friday.memory.facts.events_due_for_alert", side_effect=Exception("db gone")):
            runtime.check_event_alerts()  # must not raise


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run python -m unittest tests.test_event_alerts -v`
Expected: FAIL/ERROR — `AttributeError: module 'friday.agents.runtime' has no attribute 'check_event_alerts'`

- [ ] **Step 3: Add the sweep to `friday/agents/runtime.py`**

Insert after `_notify` (after line 911, before `async def _run_agent_job`):

```python
# ---------------------------------------------------------------------------
# Proactive event alerts (temporal facts)
# ---------------------------------------------------------------------------

_EVENTS_PSEUDO_SLUG = "events"
_last_groom_ts = 0.0


def _alert_line(event: dict) -> str:
    content = event["content"]
    window = event["window"]
    if window == "now":
        return f"Reminder: {content} is starting now."
    if window == "1h":
        return f"Reminder: {content} in about an hour."
    return f"Reminder: {content} in {int(event['minutes_until'])} minutes."


def check_event_alerts() -> None:
    """One sweep of the temporal-facts alert windows. Fires a macOS
    notification immediately and parks a pending notify so the voice
    agent mentions it on the next exchange. Never raises — this runs
    inside the scheduler tick."""
    global _last_groom_ts
    try:
        from friday.memory import facts

        now_ts = time.time()
        if now_ts - _last_groom_ts > 3600:
            _last_groom_ts = now_ts
            facts.groom_expired()

        for event in facts.events_due_for_alert():
            line = _alert_line(event)
            _mac_notification("FRIDAY — reminder", line)
            set_pending_notify(_EVENTS_PSEUDO_SLUG, line)
            emit_activity(line, kind="ok")
            facts.mark_alerted(event["id"], event["window"])
    except Exception as exc:
        logger.debug("event alert sweep skipped: %s", exc)
```

- [ ] **Step 4: Call the sweep from the scheduler loop and expose `ensure_running`**

In `_scheduler_loop` (line 1006), change:

```python
            try:
                self._tick(time.time())
            except Exception:
                logger.exception("scheduler tick failed")
```

to:

```python
            try:
                self._tick(time.time())
            except Exception:
                logger.exception("scheduler tick failed")
            check_event_alerts()
```

Add a public method on `SubagentRuntime` (after `instance()`):

```python
    def ensure_running(self) -> None:
        """Start the daemon loop + scheduler without deploying a job —
        used by server boot so event alerts fire from minute one."""
        self._ensure_loop()
```

- [ ] **Step 5: Start the runtime at server boot**

In `server.py` `_lifespan`, after the memory-reindex block:

```python
    # Start the subagent runtime so scheduled jobs and event alerts tick
    # even before any agent is deployed.
    try:
        from friday.agents.runtime import SubagentRuntime

        SubagentRuntime.instance().ensure_running()
    except Exception as exc:
        logging.getLogger("friday").warning("Runtime start skipped: %s", exc)
```

- [ ] **Step 6: Run tests**

Run: `uv run python -m unittest tests.test_event_alerts tests.test_scheduler tests.test_executor -v`
Expected: PASS — new tests green, existing scheduler/executor tests unaffected

- [ ] **Step 7: Commit**

```bash
git add friday/agents/runtime.py server.py tests/test_event_alerts.py
git commit -m "feat(runtime): proactive event alerts ride the scheduler tick"
```

---

## Phase 4 — Personalization: profile module, instant capture, context injection, reflection

### Task 9: Extract profile primitives out of the memory tool

**Files:**
- Create: `friday/memory/profile.py`
- Modify: `friday/tools/memory.py` (`update_profile` / `get_profile` delegate; `_split_frontmatter` moves out)
- Test: `tests/test_profile.py`

- [ ] **Step 1: Write the failing test**

```python
"""Tests for friday/memory/profile.py."""

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


class ProfileTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.vault_dir = Path(self._tmp.name) / "vault"

    def tearDown(self):
        self._tmp.cleanup()

    def test_update_field_creates_then_replaces(self):
        from friday.memory import profile

        profile.update_field("address_as", "boss", root=self.vault_dir)
        profile.update_field("address_as", "chief", root=self.vault_dir)
        profile.update_field("tone", "dry", root=self.vault_dir)
        text = profile.profile_text(root=self.vault_dir)
        self.assertIn("- **address_as**: chief", text)
        self.assertNotIn("boss", text)
        self.assertIn("- **tone**: dry", text)

    def test_profile_text_empty_when_missing(self):
        from friday.memory import profile

        self.assertEqual(profile.profile_text(root=self.vault_dir), "")

    def test_get_field(self):
        from friday.memory import profile

        profile.update_field("use_emojis", "no", root=self.vault_dir)
        self.assertEqual(profile.get_field("use_emojis", root=self.vault_dir), "no")
        self.assertIsNone(profile.get_field("nope", root=self.vault_dir))


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run python -m unittest tests.test_profile -v`
Expected: FAIL/ERROR — `No module named 'friday.memory.profile'`

- [ ] **Step 3: Write the implementation**

Create `friday/memory/profile.py`:

```python
"""
User profile primitives — the `- **field**: value` bullet list in
Profile/about_user.md. Extracted from friday/tools/memory.py so the
voice-agent hooks (feedback capture, reflection, context injection) can
read/write the profile without going through MCP.
"""

from __future__ import annotations

from pathlib import Path

from friday.memory import vault


PROFILE_REL = "Profile/about_user.md"


def _profile_path(root: Path | None) -> Path:
    base = root if root is not None else vault.vault_root()
    return base / PROFILE_REL


def split_frontmatter(text: str) -> tuple[str, str]:
    """Strip a leading YAML frontmatter block (---...---) if present.
    Returns (body, frontmatter_block)."""
    if not text.startswith("---"):
        return text, ""
    end = text.find("\n---", 3)
    if end < 0:
        return text, ""
    fm_end = text.find("\n", end + 4)
    if fm_end < 0:
        return "", text
    return text[fm_end + 1 :].lstrip("\n"), text[: fm_end + 1]


def profile_text(root: Path | None = None) -> str:
    """The profile body without frontmatter, or ''."""
    path = _profile_path(root)
    if not path.is_file():
        return ""
    body, _ = split_frontmatter(path.read_text(encoding="utf-8"))
    return body.strip()


def get_field(field: str, root: Path | None = None) -> str | None:
    prefix = f"- **{field}**:"
    for line in profile_text(root).splitlines():
        if line.startswith(prefix):
            return line[len(prefix):].strip()
    return None


def update_field(field: str, value: str, root: Path | None = None) -> None:
    """Insert or replace one `- **field**: value` bullet."""
    field = field.strip()
    value = value.strip()
    if not field or not value:
        return
    path = _profile_path(root)
    existing = path.read_text(encoding="utf-8") if path.is_file() else ""
    body, _ = split_frontmatter(existing)
    bullet = f"- **{field}**: {value}"
    lines = body.splitlines()
    prefix = f"- **{field}**:"
    for i, line in enumerate(lines):
        if line.startswith(prefix):
            lines[i] = bullet
            break
    else:
        if lines and lines[-1].strip():
            lines.append("")
        lines.append(bullet)
    new_body = "\n".join(lines).rstrip() + "\n"
    frontmatter = f"---\ntags: [profile]\nupdated: {vault.now_iso()}\n---\n\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(frontmatter + new_body, encoding="utf-8")
```

- [ ] **Step 4: Delegate from the MCP tools**

In `friday/tools/memory.py`:
- change the import line to `from friday.memory import vault, journal, profile as profile_mod, recall_log, search`
- replace the `update_profile` tool body (keep the docstring) with:

```python
        field = field.strip()
        value = value.strip()
        if not field or not value:
            return "Field and value are both required."
        profile_mod.update_field(field, value)
        return f"Profile updated: {field} = {value}"
```

- replace the `get_profile` tool body with:

```python
        body = profile_mod.profile_text()
        return body or "(no profile yet)"
```

- delete the now-unused module-level `_split_frontmatter` function and the `_PROFILE_PATH` constant (the profile module owns them).

- [ ] **Step 5: Run tests**

Run: `uv run python -m unittest tests.test_profile -v && uv run python -m unittest discover -s tests`
Expected: PASS, full suite green

- [ ] **Step 6: Commit**

```bash
git add friday/memory/profile.py friday/tools/memory.py tests/test_profile.py
git commit -m "refactor(memory): extract profile primitives for voice-path reuse"
```

---

### Task 10: Instant preference capture (FeedbackDetector port)

**Files:**
- Create: `friday/memory/feedback.py`
- Test: `tests/test_feedback.py`

- [ ] **Step 1: Write the failing test**

```python
"""Tests for friday/memory/feedback.py — instant preference regexes."""

import tempfile
import unittest
from pathlib import Path


class FeedbackTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.vault_dir = Path(self._tmp.name) / "vault"

    def tearDown(self):
        self._tmp.cleanup()

    def _detect(self, text):
        from friday.memory import feedback

        return feedback.detect_and_save(text, root=self.vault_dir)

    def test_call_me(self):
        from friday.memory import profile

        saved = self._detect("From now on call me Chief")
        self.assertIn(("address_as", "Chief"), saved)
        self.assertEqual(profile.get_field("address_as", root=self.vault_dir), "Chief")

    def test_style_and_emoji(self):
        self.assertIn(("response_style", "concise"), self._detect("be more concise please"))
        self.assertIn(("use_emojis", "no"), self._detect("don't use emojis"))

    def test_no_match_saves_nothing(self):
        self.assertEqual(self._detect("what's the weather like"), [])

    def test_idempotent(self):
        self._detect("call me Chief")
        self.assertEqual(self._detect("call me Chief"), [])


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run python -m unittest tests.test_feedback -v`
Expected: FAIL/ERROR — `No module named 'friday.memory.feedback'`

- [ ] **Step 3: Write the implementation**

Create `friday/memory/feedback.py`:

```python
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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run python -m unittest tests.test_feedback -v`
Expected: PASS (4 tests)

- [ ] **Step 5: Commit**

```bash
git add friday/memory/feedback.py tests/test_feedback.py
git commit -m "feat(memory): instant preference capture from user utterances"
```

---

### Task 11: Per-turn context block builder

**Files:**
- Create: `friday/memory/context.py`
- Test: `tests/test_context_block.py`

- [ ] **Step 1: Write the failing test**

```python
"""Tests for friday/memory/context.py — the per-turn injection block."""

import datetime as dt
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


class ContextBlockTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._env = patch.dict(
            os.environ, {"FRIDAY_KNOWLEDGE_DIR": self._tmp.name}, clear=False
        )
        self._env.start()
        self.vault_dir = Path(self._tmp.name) / "vault"
        (self.vault_dir / "Facts").mkdir(parents=True)

    def tearDown(self):
        self._env.stop()
        self._tmp.cleanup()

    def test_block_always_carries_current_time(self):
        from friday.memory import context

        block = context.build_block("hello", root=self.vault_dir)
        self.assertIn("Current time:", block)
        self.assertIn("internal awareness", block)

    def test_block_includes_profile_and_events_and_memory(self):
        from friday.memory import context, facts, index, profile

        profile.update_field("address_as", "Chief", root=self.vault_dir)
        facts.add_event("dentist", dt.datetime.now() + dt.timedelta(days=1))
        (self.vault_dir / "Facts" / "coffee.md").write_text(
            "# Coffee\n\nBlack coffee, no sugar.\n", encoding="utf-8"
        )
        index.reindex(vault_dir=self.vault_dir, knowledge_dir=Path(self._tmp.name) / "nokn")

        block = context.build_block("what coffee do I drink", root=self.vault_dir)
        self.assertIn("address_as", block)
        self.assertIn("TOMORROW", block)
        self.assertIn("coffee", block.lower())

    def test_due_reminder_is_marked_sent(self):
        from friday.memory import context, facts

        facts.add_event("flight", dt.datetime.now() + dt.timedelta(hours=30))
        block = context.build_block("hey", root=self.vault_dir)
        self.assertIn("REMINDER", block)
        block2 = context.build_block("hey again", root=self.vault_dir)
        self.assertNotIn("REMINDER", block2)

    def test_never_raises(self):
        from friday.memory import context

        with patch("friday.memory.search.search", side_effect=Exception("boom")):
            block = context.build_block("anything", root=self.vault_dir)
        self.assertIsInstance(block, str)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run python -m unittest tests.test_context_block -v`
Expected: FAIL/ERROR — `No module named 'friday.memory.context'`

- [ ] **Step 3: Write the implementation**

Create `friday/memory/context.py`:

```python
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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run python -m unittest tests.test_context_block -v`
Expected: PASS (4 tests)

- [ ] **Step 5: Commit**

```bash
git add friday/memory/context.py tests/test_context_block.py
git commit -m "feat(memory): per-turn context block (time, profile, events, recall)"
```

---

### Task 12: Reflection — silent LLM extraction of stable facts/preferences

**Files:**
- Create: `friday/memory/reflect.py`
- Test: `tests/test_reflect.py`

- [ ] **Step 1: Write the failing test**

```python
"""Tests for friday/memory/reflect.py — LLM extraction is mocked."""

import asyncio
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch


class ReflectTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        # housekeeping (promotion/grooming) opens the memory DB — keep it
        # inside the tempdir, never the real knowledge folder
        self._env = patch.dict(
            os.environ, {"FRIDAY_KNOWLEDGE_DIR": self._tmp.name}, clear=False
        )
        self._env.start()
        self.vault_dir = Path(self._tmp.name) / "vault"

    def tearDown(self):
        self._env.stop()
        self._tmp.cleanup()

    def test_extracted_items_land_in_profile(self):
        from friday.memory import profile, reflect

        fake = AsyncMock(
            return_value={
                "items": [
                    {"type": "fact", "key": "occupation", "value": "founder"},
                    {"type": "preference", "key": "tone", "value": "casual"},
                    {"type": "fact", "key": "", "value": "ignored"},
                ]
            }
        )
        with patch.object(reflect, "_json_call", fake):
            saved = asyncio.run(
                reflect.run(
                    ["[user]: I'm a startup founder", "[assistant]: noted"],
                    root=self.vault_dir,
                )
            )
        self.assertEqual(len(saved), 2)
        self.assertEqual(profile.get_field("occupation", root=self.vault_dir), "founder")
        self.assertEqual(profile.get_field("tone", root=self.vault_dir), "casual")

    def test_llm_failure_is_silent(self):
        from friday.memory import reflect

        with patch.object(reflect, "_json_call", AsyncMock(side_effect=Exception("api down"))):
            saved = asyncio.run(reflect.run(["[user]: hi"], root=self.vault_dir))
        self.assertEqual(saved, [])


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run python -m unittest tests.test_reflect -v`
Expected: FAIL/ERROR — `No module named 'friday.memory.reflect'`

- [ ] **Step 3: Write the implementation**

Create `friday/memory/reflect.py`:

```python
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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run python -m unittest tests.test_reflect -v`
Expected: PASS (2 tests)

- [ ] **Step 5: Commit**

```bash
git add friday/memory/reflect.py tests/test_reflect.py
git commit -m "feat(memory): silent reflection pass extracts stable facts to profile"
```

---

### Task 13: Wire the three hooks into the voice agent

**Files:**
- Modify: `agent_friday.py` (`FridayAgent.on_user_turn_completed`, line 636; new module helpers near `_handle_wake_phrase`, line 402)

No unit test for this task — `agent_friday.py` imports LiveKit at module load and is exercised live. The helpers it calls are all tested (Tasks 10–12). Verification is a live smoke step.

- [ ] **Step 1: Add module helpers**

In `agent_friday.py`, after `_handle_wake_phrase` (after line ~415), add:

```python
# ---------------------------------------------------------------------------
# Memory intelligence hooks (context injection, capture, reflection)
# ---------------------------------------------------------------------------

_turn_counter = {"n": 0}


def _recent_turns(turn_ctx, limit: int = 14) -> list[str]:
    """Best-effort transcript lines from the LiveKit ChatContext."""
    lines: list[str] = []
    try:
        for item in list(getattr(turn_ctx, "items", []))[-limit:]:
            role = getattr(item, "role", "")
            text = _chat_message_text(item)
            if role and text:
                lines.append(f"[{role}]: {text}")
    except Exception:
        pass
    return lines


def _inject_memory_context(turn_ctx, user_text: str) -> None:
    """Pre-search + profile + events block, added as an assistant-side
    context message. Must never break the voice path."""
    try:
        from friday.memory import context as memory_context

        block = memory_context.build_block(user_text)
        if block:
            turn_ctx.add_message(role="assistant", content=block)
    except Exception as exc:
        logger.debug("memory context injection skipped: %s", exc)


def _capture_and_maybe_reflect(turn_ctx, user_text: str) -> None:
    """Instant preference capture every turn; reflection every Nth."""
    try:
        from friday.memory import feedback, reflect

        feedback.detect_and_save(user_text)
        _turn_counter["n"] += 1
        if _turn_counter["n"] % reflect.reflect_every() == 0:
            transcript = _recent_turns(turn_ctx)
            asyncio.create_task(reflect.run(transcript))
    except Exception as exc:
        logger.debug("preference capture skipped: %s", exc)
```

Check the imports at the top of `agent_friday.py`: `asyncio` and `logger` are already imported/defined there (the file uses both); if `asyncio` is missing, add `import asyncio`.

- [ ] **Step 2: Call the hooks from `on_user_turn_completed`**

Change (line 636):

```python
    async def on_user_turn_completed(self, turn_ctx, new_message) -> None:
        text = getattr(new_message, "text_content", "") or _chat_message_text(new_message)
        wake = is_wake_phrase(text)
        if self._awake and wake:
            raise StopResponse()
        if self._awake:
            return
        if wake:
            self._awake = True
            return
        raise StopResponse()
```

to:

```python
    async def on_user_turn_completed(self, turn_ctx, new_message) -> None:
        text = getattr(new_message, "text_content", "") or _chat_message_text(new_message)
        wake = is_wake_phrase(text)
        if self._awake and wake:
            raise StopResponse()
        if self._awake:
            _inject_memory_context(turn_ctx, text)
            _capture_and_maybe_reflect(turn_ctx, text)
            return
        if wake:
            self._awake = True
            return
        raise StopResponse()
```

- [ ] **Step 3: Import smoke check (no LiveKit session needed)**

Run: `uv run python -c "import agent_friday; print('import ok')"`
Expected: `import ok` (plus normal env warnings if any)

- [ ] **Step 4: Live smoke (requires both processes + LiveKit creds)**

Run `uv run friday` (terminal 1) and `uv run friday_voice` (terminal 2). Wake Friday, then say: "Call me Chief. What's my schedule?"
Expected: reply addresses you per profile within a turn or two; `Profile/about_user.md` in the vault gains `- **address_as**: Chief`; no crash in the agent log. Say "remember I have a dentist appointment tomorrow at 3pm" → LLM should call `add_event` (persona wiring lands in Task 16; before that it may use `save_memory` — that's acceptable at this step).

- [ ] **Step 5: Commit**

```bash
git add agent_friday.py
git commit -m "feat(voice): per-turn memory injection, preference capture, reflection cadence"
```

---

## Phase 5 — Morning digest

### Task 14: Extract reusable news gatherers from web.py

**Files:**
- Modify: `friday/tools/web.py` (extract `gather_world_news()` / `gather_finance_news()` module-level functions from the bodies of `get_world_news` / `get_world_finance_news`)

- [ ] **Step 1: Refactor**

In `friday/tools/web.py` the two news tools live inside `register(mcp)` (lines 128 and 159). Move each body into a module-level async function directly above `register`, preserving the exact current fetching/formatting logic:

```python
async def gather_world_news() -> str:
    # ← move the current body of get_world_news here, unchanged
    ...


async def gather_finance_news() -> str:
    # ← move the current body of get_world_finance_news here, unchanged
    ...
```

(The bodies use the module's existing `httpx` + `fetch_and_parse_feed` helpers — they move verbatim; only the `def` line changes. Keep the "BRIEFING (LIVE)" output format identical.)

Then make the tools delegate:

```python
    @mcp.tool()
    async def get_world_news() -> str:
        """<keep the existing docstring exactly>"""
        return await gather_world_news()

    @mcp.tool()
    async def get_world_finance_news() -> str:
        """<keep the existing docstring exactly>"""
        return await gather_finance_news()
```

- [ ] **Step 2: Verify no behavior change**

Run: `uv run python -m unittest discover -s tests`
Expected: all green.

Run: `uv run python -c "
import asyncio
from friday.tools.web import gather_world_news
print(asyncio.run(gather_world_news())[:200])
"`
Expected: a `BRIEFING (LIVE)`-prefixed string (network required; if offline, the function's existing error path returns its fallback text — either output is a pass).

- [ ] **Step 3: Commit**

```bash
git add friday/tools/web.py
git commit -m "refactor(web): extract gather_world_news/gather_finance_news for reuse"
```

---

### Task 15: Morning digest tool

**Files:**
- Create: `friday/tools/digest.py`
- Modify: `friday/tools/__init__.py`
- Test: `tests/test_digest.py`

- [ ] **Step 1: Write the failing test**

```python
"""Tests for friday/tools/digest.py — section assembly with mocked fetchers."""

import asyncio
import datetime as dt
import os
import tempfile
import unittest
from unittest.mock import AsyncMock, patch


class FakeMCP:
    def __init__(self):
        self.tools = {}

    def tool(self):
        def decorator(fn):
            self.tools[fn.__name__] = fn
            return fn

        return decorator


class DigestTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._env = patch.dict(
            os.environ, {"FRIDAY_KNOWLEDGE_DIR": self._tmp.name}, clear=False
        )
        self._env.start()

    def tearDown(self):
        self._env.stop()
        self._tmp.cleanup()

    def _register(self):
        from friday.tools import digest

        mcp = FakeMCP()
        digest.register(mcp)
        return mcp

    def test_digest_assembles_sections_and_caches(self):
        from friday.memory import facts
        from friday.tools import digest as digest_mod

        facts.add_event("standup", dt.datetime.now() + dt.timedelta(hours=3))
        mcp = self._register()
        with patch.object(
            digest_mod, "gather_world_news", AsyncMock(return_value="BRIEFING (LIVE)\n1. Big story")
        ), patch.object(
            digest_mod, "gather_finance_news", AsyncMock(return_value="BRIEFING (LIVE)\n1. Markets up")
        ), patch.object(
            digest_mod, "_weather", AsyncMock(return_value="Sunny, 31°C in Bengaluru")
        ):
            out = asyncio.run(mcp.tools["morning_digest"]())
            self.assertIn("standup", out)
            self.assertIn("Big story", out)
            self.assertIn("Markets up", out)
            self.assertIn("Sunny", out)
            # cached: second call returns same content without re-fetching
            out2 = asyncio.run(mcp.tools["morning_digest"]())
            self.assertEqual(out, out2)

    def test_fresh_regenerates_and_failed_sections_degrade(self):
        from friday.tools import digest as digest_mod

        mcp = self._register()
        with patch.object(
            digest_mod, "gather_world_news", AsyncMock(side_effect=Exception("net down"))
        ), patch.object(
            digest_mod, "gather_finance_news", AsyncMock(return_value="BRIEFING (LIVE)\n1. Calm")
        ), patch.object(
            digest_mod, "_weather", AsyncMock(side_effect=Exception("api down"))
        ):
            out = asyncio.run(mcp.tools["morning_digest"](fresh=True))
            self.assertIn("Calm", out)
            self.assertIn("DIGEST", out)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run python -m unittest tests.test_digest -v`
Expected: FAIL/ERROR — `No module named 'friday.tools.digest'`

- [ ] **Step 3: Write the implementation**

Create `friday/tools/digest.py`:

```python
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
```

- [ ] **Step 4: Register the module**

In `friday/tools/__init__.py` add `digest` to the import tuple (after `desktop`) and add `digest.register(mcp)` to `register_all_tools` (after `events.register(mcp)`).

- [ ] **Step 5: Run test to verify it passes**

Run: `uv run python -m unittest tests.test_digest -v`
Expected: PASS (2 tests)

- [ ] **Step 6: Commit**

```bash
git add friday/tools/digest.py friday/tools/__init__.py tests/test_digest.py
git commit -m "feat(tools): morning digest composing events, weather, news, agent updates"
```

---

## Phase 6 — Persona, env, docs, closeout

### Task 16: SYSTEM_PROMPT sections

**Files:**
- Modify: `agent_friday.py` (SYSTEM_PROMPT — insert before the `## Subagents` section, around line 281)

- [ ] **Step 1: Insert three persona sections**

Add this text inside SYSTEM_PROMPT, after the `## HUD panels & camera` section and before `## Subagents`:

```markdown
## Memory awareness

Each turn you may receive a [MEMORY CONTEXT] block — current time, the boss's profile, upcoming events, and relevant memories. It is your internal awareness:
- Use it silently. Never recite it, never mention a "context block", "profile", or "memory system".
- Honor profile preferences without comment: address_as (how to address the boss), response_style, tone, use_emojis.
- The [UPCOMING EVENTS] list is for your awareness only — bring it up ONLY if asked ("what's my schedule") or when it directly matters ("can I do X tomorrow at 3?" → you know there's a conflict).
- If a [REMINDER] block appears, work it naturally into your reply once: "By the way, boss — your flight is tomorrow at two."

## Events & schedule

When the boss mentions a dated commitment ("I have a dentist appointment tomorrow at 3", "flight on Friday at 6am", "deadline next Tuesday"):
- Silently call add_event with ISO datetimes. Resolve relative dates from the current time you were given in [MEMORY CONTEXT]; call get_current_time if you have no time reference.
- One short confirmation: "Noted, boss — dentist tomorrow at three." If the tool says it overlaps another event, say so in the same breath.
- "Cancel the dentist" / "that got cancelled" → cancel_event with the keyword. "What's my schedule" / "what's coming up" → upcoming_events, speak it as a natural rundown — countdown labels, not raw lines.
- You will alert the boss automatically near event time — never promise more than that, never invent calendar integrations.

## Morning digest

When the boss says "good morning", "morning briefing", or asks to start the day:
- Silently call morning_digest. Narrate it as ONE flowing briefing in your voice: greeting, today's events, weather, the two or three biggest world stories, markets in one line, and anything agents found overnight. 6–9 sentences, no lists, no markdown, no section names.
- If he wants depth on the news afterwards, the world monitor dance applies as usual (brief first, then "Let me open up the world monitor for you." + open_world_monitor).
- "Fresh digest" / "rebuild the briefing" → morning_digest with fresh=true.
```

- [ ] **Step 2: Import smoke check**

Run: `uv run python -c "import agent_friday; print(len(agent_friday.SYSTEM_PROMPT))"`
Expected: prints a length (no syntax errors); number is larger than before.

- [ ] **Step 3: Commit**

```bash
git add agent_friday.py
git commit -m "feat(persona): memory awareness, events, and morning digest sections"
```

---

### Task 17: Env knobs, docs, full verification

**Files:**
- Modify: `.env.example`
- Modify: `CLAUDE.md` (Friday v2 section)

- [ ] **Step 1: Document the new knobs in `.env.example`**

Append (matching the file's existing comment style):

```bash
# --- Memory intelligence (Tier A port, 2026-06) ---
# Per-turn memory injection: how many search hits ride along each turn.
FRIDAY_PRESEARCH_RESULTS=4
# Reflection cadence: extract stable facts/preferences every N turns.
FRIDAY_REFLECT_EVERY_TURNS=12
# Promotion threshold: recall score at which a memory crystallizes
# into Profile/promoted.md (0..1).
FRIDAY_PROMOTE_THRESHOLD=0.55
```

- [ ] **Step 2: Update CLAUDE.md**

In the `## Friday v2: trust mode + executor` section, append one bullet:

```markdown
- **Memory intelligence** (`friday/memory/`): SQLite at `<FRIDAY_KNOWLEDGE_DIR>/_memory/memory.db` — FTS5 note index (`db.py`/`index.py`/`search.py` with scan fallback), recall ledger + promotion to `Profile/promoted.md` (`recall_log.py`), temporal facts with conflict linting + 1h/15m/now alert windows riding the runtime scheduler tick (`facts.py`, alerts in `agents/runtime.py:check_event_alerts`), per-turn context injection + instant preference capture + every-12-turns reflection wired in `agent_friday.py:on_user_turn_completed`. New tools: `events.py` (add/cancel/upcoming), `digest.py` (morning_digest, keyless wttr.in weather). Voice-path hooks must never raise — keep the try/except wrappers.
```

- [ ] **Step 3: Full suite + tool catalog smoke**

Run: `uv run python -m unittest discover -s tests`
Expected: all green (≈ previous count + ~25 new tests).

Run: `uv run python -c "
from mcp.server.fastmcp import FastMCP
from friday.tools import register_all_tools
m = FastMCP(name='probe')
register_all_tools(m)
import asyncio
tools = asyncio.run(m.list_tools())
names = {t.name for t in tools}
assert {'add_event','cancel_event','upcoming_events','morning_digest'} <= names, names
print(f'{len(tools)} tools registered, new tools present')
"`
Expected: `<N> tools registered, new tools present` (N ≈ 94).

- [ ] **Step 4: Live end-to-end dogfood (manual, both processes running)**

1. "Wake up" → "Good morning" → spoken digest with weather + events + news, one flowing narration.
2. "I have a dentist appointment tomorrow at 3pm" → confirmation; `upcoming_events` shows TOMORROW label; macOS notification fires when a test event is set 15 minutes out.
3. "Call me Chief" → next replies use "Chief"; `Profile/about_user.md` updated.
4. "What did I tell you about coffee?" → answer arrives without an explicit `search_memory` call (context injection did it).

- [ ] **Step 5: Commit**

```bash
git add .env.example CLAUDE.md
git commit -m "docs: env knobs + CLAUDE.md for Tier A memory intelligence"
```

---

## Out of scope (explicitly deferred)

- Embeddings/vector search and MMR rerank (Tier B — FTS5 + scan covers Tier A; the `db.py` schema doesn't block adding a vectors table later).
- Cron expressions for the executor scheduler, operative state persistence, ApprovalStore learned permissions, dreaming job, session compaction (Tier B).
- Gmail/GCalendar/GitHub connectors, TTS-voice digest persona files (Tier C — credentials in hand first, per CLAUDE.md).
- MemOS-style PrivacyManager/SafetyFilter (Friday's existing security suite covers its threat model for now).

## Task dependency notes for executors

- Tasks 1→2→3 are strictly sequential (db → index → search).
- Task 4 needs 1+3; Task 5 needs 3+4.
- Tasks 6→7→8 are sequential within Phase 3 but only depend on Task 1 otherwise.
- Tasks 9→10/11/12 — 9 first, then 10/11/12 in any order; Task 11 needs 3+4+6; Task 13 needs 10+11+12.
- Task 14→15 sequential; Task 15 also needs 6 (events section) and benefits from 8 (agent news, but degrades gracefully without).
- Tasks 16–17 last.
