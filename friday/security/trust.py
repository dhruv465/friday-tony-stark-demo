"""Trust mode — session-scoped permission gate for Tier 1 actions.

The boss can arm "trust mode" for a bounded window ("trust mode on").
While armed, Tier 1 brokers (shell, local apps, subagents, …) skip the
per-action confirm dance and execute immediately. Tier 2 actions
(messaging other people, security remediation, anything irreversible)
ALWAYS require explicit confirmation — trust mode never silences them,
and the tier assignment is hardcoded here, not configurable at runtime.

State is a single JSON file at ``<FRIDAY_KNOWLEDGE_DIR>/_trust/state.json``
so the MCP server, the desktop Brain, and the executor's worker thread
all observe the same answer. Every read re-checks ``expires_at`` —
an expired file means disarmed, no daemon needed.

Arming is only ever triggered by the boss's explicit spoken/typed
command; the enable tool's docstring and the agent SYSTEM_PROMPT forbid
the LLM from self-arming.
"""

from __future__ import annotations

import json
import logging
import os
import time
from datetime import datetime, timedelta
from pathlib import Path


logger = logging.getLogger("friday.security.trust")

STATE_JSON = "state.json"

DEFAULT_MINUTES = 30
MAX_MINUTES = 240

# Tool tiers. Tier assignments live with the brokers that consult them;
# these constants just name the contract.
TIER_FREE = 0        # read-only: search, fetch, recall, status
TIER_TRUSTED = 1     # trust mode OR per-action confirm
TIER_ALWAYS_CONFIRM = 2  # confirm even when armed — hard floor


def _trust_dir() -> Path:
    from friday.learning.store import knowledge_root

    folder = knowledge_root() / "_trust"
    folder.mkdir(parents=True, exist_ok=True)
    return folder


def _state_path() -> Path:
    return _trust_dir() / STATE_JSON


def _atomic_write(path: Path, text: str) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def _emit(detail: str, kind: str = "info") -> None:
    try:
        from friday.desktop.events import append_event

        append_event("activity", source="trust", tool="trust_mode", detail=detail[:180], kind=kind)
    except Exception as exc:
        logger.debug("trust activity emit skipped: %s", exc)


def default_minutes() -> int:
    try:
        minutes = int(os.getenv("FRIDAY_TRUST_DEFAULT_MINUTES", str(DEFAULT_MINUTES)))
    except ValueError:
        minutes = DEFAULT_MINUTES
    return max(1, min(minutes, MAX_MINUTES))


def _load_state() -> dict:
    path = _state_path()
    if not path.is_file():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def _save_state(state: dict) -> None:
    _atomic_write(_state_path(), json.dumps(state, indent=2))


def arm(duration_minutes: int | None = None, scope: str = "") -> dict:
    """Arm trust mode for a bounded window. Returns the new status."""
    minutes = duration_minutes if duration_minutes and duration_minutes > 0 else default_minutes()
    minutes = min(int(minutes), MAX_MINUTES)
    now = datetime.now()
    state = {
        "armed": True,
        "armed_at": now.isoformat(timespec="seconds"),
        "expires_at": (now + timedelta(minutes=minutes)).isoformat(timespec="seconds"),
        "expires_ts": time.time() + minutes * 60,
        "scope": scope.strip(),
    }
    _save_state(state)
    _emit(f"trust mode armed for {minutes} min", kind="ok")
    return status()


def disarm(reason: str = "disabled by the boss") -> dict:
    """Disarm trust mode immediately."""
    state = _load_state()
    if state.get("armed"):
        _save_state({"armed": False, "disarmed_at": datetime.now().isoformat(timespec="seconds")})
        _emit(f"trust mode off — {reason}")
    return status()


def is_armed() -> bool:
    """True iff trust mode is armed and not expired. Reads disk each call."""
    state = _load_state()
    if not state.get("armed"):
        return False
    if time.time() >= float(state.get("expires_ts", 0)):
        # Lazy expiry: first caller after the deadline flips the file so
        # the HUD event fires exactly once.
        _save_state({"armed": False, "disarmed_at": datetime.now().isoformat(timespec="seconds")})
        _emit("trust mode expired")
        return False
    return True


def status() -> dict:
    state = _load_state()
    armed = is_armed()
    result = {"armed": armed}
    if armed:
        remaining = max(0, int(float(state.get("expires_ts", 0)) - time.time()))
        result.update(
            {
                "armed_at": state.get("armed_at"),
                "expires_at": state.get("expires_at"),
                "remaining_seconds": remaining,
                "scope": state.get("scope", ""),
            }
        )
    return result


def trusted_short_circuit(confirm, action_id: str) -> dict | None:
    """If trust mode is armed, execute a staged Tier 1 action immediately.

    Brokers call this right after staging: ``confirm`` is their own
    confirm function. Returns the confirm result (annotated with
    ``trust_mode: True``) when armed, or None when the normal
    pending-confirmation flow should proceed. Tier 2 brokers must NOT
    call this.
    """
    if not is_armed():
        return None
    result = confirm(action_id)
    if isinstance(result, dict):
        result["trust_mode"] = True
    return result
