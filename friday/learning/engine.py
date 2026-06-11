"""
Learning engine — background topic research jobs.

Why a dedicated thread + private event loop: the desktop Brain executes
every MCP tool via ``asyncio.run(...)`` — a throwaway loop per call — so
``asyncio.create_task`` from a tool dies the moment the call returns.
``LearningRuntime`` owns a daemon thread running its own persistent loop;
jobs are scheduled onto it with ``run_coroutine_threadsafe`` and survive
the caller. The same mechanism works inside the FastMCP SSE server.

Cross-process safety (the SSE server and the desktop Brain can both host
these tools): a per-topic ``runner.lock`` (pid + heartbeat) prevents
duplicate jobs, and ``job.json`` on disk is the pause/stop channel —
re-read before every round, so either process can pause a job the other
one is running.

Failure posture: a failed round backs off exponentially; three consecutive
failures mark the job ``stalled`` with a retry-after timestamp. Stalled is
never dead — ``resume_in_progress()`` (server startup) and
``continue_learning`` pick it back up.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import threading
from datetime import datetime, timedelta

import httpx

from friday.config import config
from friday.learning import extract, llm, store
from friday.tools import web


logger = logging.getLogger("friday.learning")

STALL_FAILURES = 3
STALL_BACKOFF_MIN = 30


def _max_rounds() -> int:
    return int(os.getenv("FRIDAY_LEARNER_MAX_ROUNDS", config.FRIDAY_LEARNER_MAX_ROUNDS))


def _max_pages_per_round() -> int:
    return int(
        os.getenv(
            "FRIDAY_LEARNER_MAX_PAGES_PER_ROUND",
            config.FRIDAY_LEARNER_MAX_PAGES_PER_ROUND,
        )
    )


def _fetch_delay_s() -> float:
    return float(
        os.getenv("FRIDAY_LEARNER_FETCH_DELAY_S", config.FRIDAY_LEARNER_FETCH_DELAY_S)
    )


def _max_concurrent() -> int:
    return int(
        os.getenv("FRIDAY_LEARNER_MAX_CONCURRENT", config.FRIDAY_LEARNER_MAX_CONCURRENT)
    )


def _backoff_base_s() -> float:
    return float(os.getenv("FRIDAY_LEARNER_BACKOFF_BASE_S", "10"))


def emit_activity(detail: str, kind: str = "info") -> None:
    """HUD activity row. Never raises — `_reject_secrets` can throw on
    secret-looking page titles/URLs."""
    try:
        from friday.desktop.events import append_event

        append_event(
            "activity", source="learning", tool="learning", detail=detail[:180], kind=kind
        )
    except Exception as exc:
        logger.debug("learning activity emit skipped: %s", exc)


def _save_preserving_control(slug: str, job: dict) -> None:
    """Save job state without clobbering a pause/stop another process (or
    the user, mid-round) wrote to disk while this round was running."""
    disk = store.load_job(slug)
    if disk and disk.get("status") in ("paused", "stopped"):
        job["status"] = disk["status"]
    store.save_job(slug, job)


_QUESTION_BULLET = re.compile(r"^[-*]\s*(?:\[.\]\s*)?")


def _normalize_url(url: str) -> str:
    from urllib.parse import urlparse, urlunparse

    parsed = urlparse(url.strip())
    path = parsed.path.rstrip("/")
    return urlunparse(
        (parsed.scheme.lower() or "https", parsed.netloc.lower(), path, "", parsed.query, "")
    )


async def _run_round(client: httpx.AsyncClient, job: dict, robots_cache: dict) -> bool:
    """One learning round. Mutates + saves ``job``. Returns False on failure."""
    slug = job["slug"]
    topic = job["topic"]
    index_md = store.read_doc(slug, "index.md")
    questions_md = store.read_doc(slug, "open-questions.md")

    try:
        plan = await llm.generate_queries(
            topic, index_md, questions_md, job.get("queries_run", [])
        )
    except llm.LLMFailure as exc:
        job["last_error"] = str(exc)
        return False

    if plan["done"]:
        job["coverage"] = "good"
        _save_preserving_control(slug, job)
        return True

    seen = {_normalize_url(u) for u in job.get("sources_seen", [])}
    candidates: list[tuple[str, str]] = []  # (title, normalized url)
    used_queries: list[str] = []
    for query in plan["queries"]:
        used_queries.append(query)
        try:
            results = await web.ddg_search_raw(query, 6)
        except Exception as exc:
            logger.debug("search failed for %r: %s", query, exc)
            continue
        for title, link, _snippet in results:
            normalized = _normalize_url(link)
            if normalized in seen:
                continue
            seen.add(normalized)
            candidates.append((title, normalized))
    candidates = candidates[: _max_pages_per_round()]

    pages: list[dict] = []
    for title, url in candidates:
        try:
            if not await extract.allowed_by_robots(client, url, robots_cache):
                continue
            await asyncio.sleep(_fetch_delay_s())
            text = await extract.fetch_page_text(client, url)
        except Exception as exc:
            logger.debug("fetch failed for %s: %s", url, exc)
            continue
        if text.strip():
            pages.append({"title": title, "url": url, "text": text})

    if not pages:
        if not candidates:
            # Coverage plateau: the searches surfaced nothing new. Take the
            # planner's coverage assessment and exit the round gracefully
            # instead of spinning on an exhausted topic.
            job["coverage"] = plan["coverage"]
            job["queries_run"] = job.get("queries_run", []) + used_queries
            _save_preserving_control(slug, job)
            emit_activity(f"'{topic}': no new sources found, coverage {job['coverage']}")
            return True
        job["last_error"] = "all page fetches failed this round"
        return False

    notes_dir = store.topic_dir(slug) / "notes"
    existing_slugs = sorted(p.stem for p in notes_dir.glob("*.md")) if notes_dir.is_dir() else []
    try:
        result = await llm.distill(topic, index_md, questions_md, pages, existing_slugs)
    except llm.LLMFailure as exc:
        job["last_error"] = str(exc)
        return False

    rounds = job.get("rounds_completed", 0) + 1
    store.write_doc(
        slug,
        "index.md",
        result["index_md"],
        frontmatter={
            "topic": topic,
            "coverage": result["coverage"],
            "rounds": rounds,
            "updated": store.vault.now_iso(),
        },
    )
    store.write_doc(slug, "open-questions.md", result["open_questions_md"])
    for note in result["subtopic_notes"]:
        note_slug = store.vault.slugify(note.get("slug") or note.get("title") or "note")
        store.write_doc(
            slug,
            f"notes/{note_slug}.md",
            note["md"],
            frontmatter={"topic": topic, "title": note.get("title", note_slug)},
        )
    store.append_sources(slug, [(p["title"], p["url"]) for p in pages])

    job["sources_seen"] = job.get("sources_seen", []) + [p["url"] for p in pages]
    job["queries_run"] = job.get("queries_run", []) + used_queries
    job["rounds_completed"] = rounds
    job["rounds_this_session"] = job.get("rounds_this_session", 0) + 1
    job["coverage"] = result["coverage"]
    job["open_questions"] = [
        _QUESTION_BULLET.sub("", line.strip())
        for line in result["open_questions_md"].splitlines()
        if line.strip().startswith(("-", "*"))
    ]
    job["last_error"] = None
    _save_preserving_control(slug, job)

    emit_activity(
        f"'{topic}' round {rounds}: +{len(pages)} pages, coverage {job['coverage']}, "
        f"{len(job['open_questions'])} open questions",
        kind="ok",
    )
    return True


async def _run_job(topic: str, slug: str) -> None:
    if not store.acquire_runner_lock(slug):
        emit_activity(f"'{topic}': another FRIDAY process is already on it")
        return

    job = store.load_job(slug) or store.new_job(topic, slug)
    job["status"] = "running"
    job["sessions"] = job.get("sessions", 0) + 1
    job["rounds_this_session"] = 0
    job["consecutive_failures"] = 0
    job["stalled_until"] = None
    store.save_job(slug, job)
    emit_activity(f"learning '{topic}' — session {job['sessions']} started")

    try:
        async with httpx.AsyncClient(follow_redirects=True, timeout=15) as client:
            robots_cache: dict = {}
            while True:
                job = store.load_job(slug) or job  # cross-process pause/stop wins
                if job["status"] in ("paused", "stopped"):
                    break
                if job.get("rounds_this_session", 0) >= _max_rounds():
                    break
                ok = await _run_round(client, job, robots_cache)
                store.heartbeat(slug)
                if not ok:
                    job["consecutive_failures"] = job.get("consecutive_failures", 0) + 1
                    if job["consecutive_failures"] >= STALL_FAILURES:
                        job["status"] = "stalled"
                        job["stalled_until"] = (
                            datetime.now() + timedelta(minutes=STALL_BACKOFF_MIN)
                        ).isoformat(timespec="seconds")
                        _save_preserving_control(slug, job)
                        break
                    _save_preserving_control(slug, job)
                    await asyncio.sleep(
                        _backoff_base_s() * (2 ** job["consecutive_failures"])
                    )
                    continue
                job["consecutive_failures"] = 0
                if job.get("coverage") == "good":
                    job["status"] = "complete"
                    _save_preserving_control(slug, job)
                    break
        final = store.load_job(slug) or job
        kind = "err" if final["status"] == "stalled" else "ok"
        emit_activity(f"'{topic}' session ended — {final['status']}", kind=kind)
    except Exception as exc:  # never let a job kill the runtime loop
        logger.exception("learning job %r crashed", topic)
        job["status"] = "stalled"
        job["last_error"] = str(exc)
        job["stalled_until"] = (
            datetime.now() + timedelta(minutes=STALL_BACKOFF_MIN)
        ).isoformat(timespec="seconds")
        store.save_job(slug, job)
        emit_activity(f"'{topic}' hit an error — will retry later", kind="err")
    finally:
        store.release_runner_lock(slug)


class LearningRuntime:
    """Singleton owning the background-learning thread + event loop."""

    _instance: "LearningRuntime | None" = None
    _instance_lock = threading.Lock()

    def __init__(self) -> None:
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._futures: dict[str, "asyncio.Future"] = {}
        self._semaphore: asyncio.Semaphore | None = None

    @classmethod
    def instance(cls) -> "LearningRuntime":
        with cls._instance_lock:
            if cls._instance is None:
                cls._instance = cls()
            return cls._instance

    def _ensure_loop(self) -> asyncio.AbstractEventLoop:
        if self._loop is not None and self._thread is not None and self._thread.is_alive():
            return self._loop
        loop = asyncio.new_event_loop()

        def _runner() -> None:
            asyncio.set_event_loop(loop)
            loop.run_forever()

        thread = threading.Thread(target=_runner, name="friday-learning", daemon=True)
        thread.start()
        self._loop = loop
        self._thread = thread
        self._semaphore = asyncio.Semaphore(_max_concurrent())
        return loop

    async def _guarded(self, topic: str, slug: str) -> None:
        assert self._semaphore is not None
        async with self._semaphore:
            await _run_job(topic, slug)

    def is_active(self, slug: str) -> bool:
        future = self._futures.get(slug)
        return future is not None and not future.done()

    def start_job(self, topic: str, *, resumed: bool = False) -> dict:
        slug = store.slug_for(topic)
        if self.is_active(slug):
            return {"status": "already_active", "topic": topic, "slug": slug}
        if store.lock_is_live(slug):
            return {"status": "already_active", "topic": topic, "slug": slug}
        loop = self._ensure_loop()
        future = asyncio.run_coroutine_threadsafe(self._guarded(topic, slug), loop)
        self._futures[slug] = future
        return {
            "status": "learning_resumed" if resumed else "learning_started",
            "topic": topic,
            "slug": slug,
        }

    def request_pause(self, slug: str) -> dict:
        return self._set_status(slug, "paused")

    def request_stop(self, slug: str) -> dict:
        return self._set_status(slug, "stopped")

    def _set_status(self, slug: str, status: str) -> dict:
        job = store.load_job(slug)
        if job is None:
            return {"status": "unknown_topic", "slug": slug}
        job["status"] = status
        store.save_job(slug, job)
        return {"status": status, "topic": job["topic"], "slug": slug}

    def resume_in_progress(self) -> list[str]:
        """Pick up jobs left ``running`` by a dead process (or ``stalled``
        past their backoff). Called from the server lifespan hook."""
        resumed: list[str] = []
        now_iso = store.vault.now_iso()
        for job in store.list_jobs():
            slug = job.get("slug", "")
            if not slug or self.is_active(slug) or store.lock_is_live(slug):
                continue
            status = job.get("status")
            stalled_ready = (
                status == "stalled" and (job.get("stalled_until") or "") <= now_iso
            )
            if status == "running" or stalled_ready:
                self.start_job(job["topic"], resumed=True)
                resumed.append(slug)
        return resumed
