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
