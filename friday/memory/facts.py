"""
Temporal facts — dated events with conflict linting, day-before
reminders, and minute-level alert windows. Port of the MemOS FactStore +
ProactiveEventsWatcher mechanics onto Friday's shared memory.db. Alert
dedup lives in columns (alerted_1h/15m/now), not a side file.
All datetimes are naive local time; DST transitions can shift alert windows by ±1 hour.
"""

from __future__ import annotations

import datetime as _dt
import uuid

from friday.memory import db


def _now_iso() -> str:
    return _dt.datetime.now().isoformat(timespec="seconds")


def _as_iso(value: _dt.datetime) -> str:
    if value.tzinfo is not None:
        # Convert to local wall time, then store naive — every stored
        # string must sort against naive local `_now_iso()` bounds.
        value = value.astimezone().replace(tzinfo=None)
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
    return ls < re_ and rs < le


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
        count = cur.rowcount
    finally:
        conn.close()
    if count:
        # An expiry can dissolve a conflict pair — re-derive contested flags.
        lint_conflicts()
    return count


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
        if days_until < 0:
            label = f"ONGOING until {de.strftime('%a %b %d %I:%M %p')}"
        elif days_until == 0:
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
