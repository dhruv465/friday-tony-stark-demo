"""
Knowledge store — FRIDAY-managed topic folders on disk.

Lives OUTSIDE the Obsidian memory vault (which is hard-rooted at
``OBSIDIAN_VAULT_PATH/Memory``). The knowledge root defaults to a
machine-managed folder inside SecBrain; the human wiki is never touched.

Per-topic layout::

    <FRIDAY_KNOWLEDGE_DIR>/
      <topic-slug>/
        job.json            # learning job state
        runner.lock         # {"pid": int, "heartbeat": unix_ts}
        index.md            # distilled overview (frontmatter: topic, coverage, ...)
        open-questions.md   # "- [ ] question" bullets
        sources.md          # consumed pages, one bullet each
        notes/<subtopic>.md

All writes are atomic (tmp + ``os.replace``) — Obsidian may have the
folder open while FRIDAY writes.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

from friday.config import config
from friday.memory import search as memory_search
from friday.memory import vault


LOCK_STALE_S = 120
LOCK_NAME = "runner.lock"
JOB_NAME = "job.json"

_DOC_NAMES = {"index.md", "open-questions.md", "sources.md"}


class KnowledgeError(ValueError):
    """Raised when a caller asks for a path outside the knowledge root."""


def knowledge_root() -> Path:
    """Return the knowledge folder, creating it on first use.

    Reads the env var per call (not just at import) so tests can point
    at a tmpdir — same precedent as ``shell._resolve_cwd``.
    """
    raw = os.getenv("FRIDAY_KNOWLEDGE_DIR", config.FRIDAY_KNOWLEDGE_DIR)
    root = Path(raw).expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
    return root


def slug_for(topic: str) -> str:
    return vault.slugify(topic)


def topic_dir(slug: str, create: bool = True) -> Path:
    if not slug:
        raise KnowledgeError("empty topic slug")
    root = knowledge_root()
    candidate = (root / slug).resolve()
    try:
        rel = candidate.relative_to(root)
    except ValueError:
        raise KnowledgeError(f"path escapes knowledge root: {slug!r}")
    if len(rel.parts) != 1:
        raise KnowledgeError(f"topic slug must be a single folder name: {slug!r}")
    if create:
        candidate.mkdir(parents=True, exist_ok=True)
    return candidate


def _doc_path(slug: str, name: str) -> Path:
    base = topic_dir(slug)
    candidate = (base / name).resolve()
    try:
        candidate.relative_to(base)
    except ValueError:
        raise KnowledgeError(f"doc path escapes topic folder: {name!r}")
    if candidate.suffix != ".md" or not (
        name in _DOC_NAMES or name.startswith("notes/")
    ):
        raise KnowledgeError(f"unsupported doc name: {name!r}")
    return candidate


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


# ---------------------------------------------------------------------------
# Job state
# ---------------------------------------------------------------------------

def new_job(topic: str, slug: str) -> dict:
    now = vault.now_iso()
    return {
        "topic": topic,
        "slug": slug,
        "status": "running",
        "created_at": now,
        "updated_at": now,
        "sessions": 0,
        "rounds_completed": 0,
        "rounds_this_session": 0,
        "sources_seen": [],
        "queries_run": [],
        "open_questions": [],
        "coverage": "shallow",
        "consecutive_failures": 0,
        "last_error": None,
        "stalled_until": None,
    }


def load_job(slug: str) -> dict | None:
    path = topic_dir(slug, create=False) / JOB_NAME
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def save_job(slug: str, job: dict) -> None:
    job["updated_at"] = vault.now_iso()
    _atomic_write(topic_dir(slug) / JOB_NAME, json.dumps(job, indent=2))


def list_jobs() -> list[dict]:
    root = knowledge_root()
    jobs = []
    for path in sorted(root.glob(f"*/{JOB_NAME}")):
        try:
            jobs.append(json.loads(path.read_text(encoding="utf-8")))
        except (json.JSONDecodeError, OSError):
            continue
    return jobs


def known_topics() -> list[dict]:
    jobs = list_jobs()
    jobs.sort(key=lambda j: j.get("updated_at") or "", reverse=True)
    return [
        {
            "topic": j.get("topic", j.get("slug", "")),
            "slug": j.get("slug", ""),
            "status": j.get("status", "unknown"),
            "coverage": j.get("coverage", "shallow"),
            "rounds_completed": j.get("rounds_completed", 0),
            "updated_at": j.get("updated_at"),
        }
        for j in jobs
    ]


# ---------------------------------------------------------------------------
# Docs
# ---------------------------------------------------------------------------

def read_doc(slug: str, name: str) -> str:
    try:
        path = _doc_path(slug, name)
    except KnowledgeError:
        return ""
    if not path.is_file():
        return ""
    return path.read_text(encoding="utf-8")


def write_doc(slug: str, name: str, body: str, frontmatter: dict | None = None) -> Path:
    path = _doc_path(slug, name)
    content = vault._render_frontmatter(frontmatter) + body if frontmatter else body
    _atomic_write(path, content)
    return path


def append_sources(slug: str, entries: list[tuple[str, str]]) -> None:
    if not entries:
        return
    path = topic_dir(slug) / "sources.md"
    now = vault.now_iso()
    existing = path.read_text(encoding="utf-8") if path.is_file() else "# Sources\n\n"
    lines = [f"- [{title or url}]({url}) — fetched {now}" for title, url in entries]
    _atomic_write(path, existing.rstrip("\n") + "\n" + "\n".join(lines) + "\n")


# ---------------------------------------------------------------------------
# Runner lock — cross-process duplicate-job guard
# ---------------------------------------------------------------------------

def _lock_path(slug: str) -> Path:
    return topic_dir(slug) / LOCK_NAME


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except (PermissionError, OSError):
        return True
    return True


def lock_is_live(slug: str) -> bool:
    """True if another live process holds this topic's runner lock."""
    path = _lock_path(slug)
    if not path.is_file():
        return False
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return False
    if data.get("pid") == os.getpid():
        return False
    if time.time() - float(data.get("heartbeat", 0)) > LOCK_STALE_S:
        return False
    return _pid_alive(int(data.get("pid", -1)))


def acquire_runner_lock(slug: str) -> bool:
    if lock_is_live(slug):
        return False
    heartbeat(slug)
    return True


def heartbeat(slug: str) -> None:
    _atomic_write(
        _lock_path(slug),
        json.dumps({"pid": os.getpid(), "heartbeat": time.time()}),
    )


def release_runner_lock(slug: str) -> None:
    try:
        _lock_path(slug).unlink()
    except FileNotFoundError:
        pass


# ---------------------------------------------------------------------------
# Recall
# ---------------------------------------------------------------------------

def recall(query: str, k: int = 5) -> list[memory_search.Hit]:
    """Keyword search across all knowledge notes (not the memory vault)."""
    return memory_search.search(query, k=k, root=knowledge_root())
