"""
Incremental indexer: walks the Obsidian vault and the knowledge dir,
mirrors every visible .md file into the notes_fts FTS5 table. mtime-diff
against note_meta keeps re-runs cheap. Underscore-prefixed folders
(_agents, _trust, _memory) and Obsidian internals are skipped.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from pathlib import Path

from friday.config import config
from friday.memory import db, vault


logger = logging.getLogger("friday.memory.index")

_SKIP_PARTS_PREFIX = ("_", ".")
_STALE_INTERVAL_S = 60.0
_last_reindex_ts = 0.0
_reindex_lock = threading.Lock()


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
    """Throttled reindex — at most once per minute, never raises. The
    timestamp only advances on success so a transient failure (missing
    DB, bad vault path) retries on the next call instead of going
    silent for a full interval."""
    global _last_reindex_ts
    now = time.time()
    if now - _last_reindex_ts < _STALE_INTERVAL_S:
        return
    with _reindex_lock:
        if now - _last_reindex_ts < _STALE_INTERVAL_S:
            return
        try:
            reindex()
            _last_reindex_ts = time.time()
        except Exception as exc:
            logger.warning("reindex failed: %s", exc)
