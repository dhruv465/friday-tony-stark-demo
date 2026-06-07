"""Confirmed local Mac app actions for Notes and Reminders."""

from __future__ import annotations

import subprocess
import time
import uuid
from dataclasses import dataclass

from friday.tools.desktop import _reject_secret_content


PENDING_TTL_SECONDS = 600
PENDING_LOCAL_APP_ACTIONS: dict[str, "PendingLocalAppAction"] = {}


@dataclass
class PendingLocalAppAction:
    action_id: str
    kind: str
    summary: str
    created_at: float
    payload: dict


def clear_pending_local_app_actions() -> None:
    PENDING_LOCAL_APP_ACTIONS.clear()


def _apple_string(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


def _osascript(script: str) -> subprocess.CompletedProcess:
    return subprocess.run(["osascript", "-e", script], check=True)


def _prune_expired() -> None:
    now = time.time()
    expired = [
        action_id
        for action_id, action in PENDING_LOCAL_APP_ACTIONS.items()
        if now - action.created_at > PENDING_TTL_SECONDS
    ]
    for action_id in expired:
        PENDING_LOCAL_APP_ACTIONS.pop(action_id, None)


def _latest_pending_id() -> str:
    _prune_expired()
    if not PENDING_LOCAL_APP_ACTIONS:
        raise KeyError("No pending local app action.")
    return max(PENDING_LOCAL_APP_ACTIONS.values(), key=lambda action: action.created_at).action_id


def _clean_required(value: str, label: str) -> str:
    cleaned = (value or "").strip()
    if not cleaned:
        raise ValueError(f"{label} is required.")
    _reject_secret_content(cleaned)
    return cleaned


def _stage(kind: str, summary: str, payload: dict) -> dict:
    action_id = uuid.uuid4().hex
    PENDING_LOCAL_APP_ACTIONS[action_id] = PendingLocalAppAction(
        action_id=action_id,
        kind=kind,
        summary=summary,
        created_at=time.time(),
        payload=payload,
    )
    return {
        "status": "pending_confirmation",
        "action_id": action_id,
        "risk": f"create_{kind}",
        "summary": summary,
        "expires_in_seconds": PENDING_TTL_SECONDS,
    }


def prepare_local_note(title: str, body: str = "", folder: str = "Notes") -> dict:
    clean_title = _clean_required(title, "Title")
    clean_body = (body or "").strip()
    clean_folder = (folder or "Notes").strip() or "Notes"
    _reject_secret_content(clean_body)
    _reject_secret_content(clean_folder)
    return _stage(
        "note",
        f"Create local Notes note '{clean_title}' in {clean_folder}",
        {"title": clean_title, "body": clean_body, "folder": clean_folder},
    )


def prepare_local_reminder(title: str, notes: str = "", list_name: str = "Reminders") -> dict:
    clean_title = _clean_required(title, "Title")
    clean_notes = (notes or "").strip()
    clean_list = (list_name or "Reminders").strip() or "Reminders"
    _reject_secret_content(clean_notes)
    _reject_secret_content(clean_list)
    return _stage(
        "reminder",
        f"Create local Reminders item '{clean_title}' in {clean_list}",
        {"title": clean_title, "notes": clean_notes, "list_name": clean_list},
    )


def confirm_local_app_action(action_id: str | None = None) -> dict:
    if not action_id:
        action_id = _latest_pending_id()
    action = PENDING_LOCAL_APP_ACTIONS.get(action_id)
    if action is None:
        raise KeyError(f"No pending local app action: {action_id}")
    if time.time() - action.created_at > PENDING_TTL_SECONDS:
        PENDING_LOCAL_APP_ACTIONS.pop(action_id, None)
        raise TimeoutError(f"Pending local app action expired: {action_id}")

    if action.kind == "note":
        result = _create_note(action.payload)
    elif action.kind == "reminder":
        result = _create_reminder(action.payload)
    else:
        raise ValueError(f"Unsupported local app action: {action.kind}")

    PENDING_LOCAL_APP_ACTIONS.pop(action_id, None)
    return {
        "status": "executed",
        "action_id": action_id,
        "summary": action.summary,
        "result": result,
    }


def cancel_local_app_action(action_id: str | None = None) -> dict:
    if not action_id:
        try:
            action_id = _latest_pending_id()
        except KeyError:
            return {"status": "noop", "reason": "no pending local app action"}
    removed = PENDING_LOCAL_APP_ACTIONS.pop(action_id, None)
    return {"status": "cancelled" if removed else "noop", "action_id": action_id}


def _create_note(payload: dict) -> str:
    title = _apple_string(payload["title"])
    body = _apple_string(payload.get("body", ""))
    folder = _apple_string(payload.get("folder", "Notes"))
    script = f'''
tell application "Notes"
    activate
    set targetFolder to missing value
    repeat with targetAccount in accounts
        try
            set targetFolder to folder "{folder}" of targetAccount
            exit repeat
        end try
    end repeat
    if targetFolder is missing value then
        set targetFolder to folder "Notes" of default account
    end if
    make new note at targetFolder with properties {{name:"{title}", body:"{body}"}}
end tell
'''
    _osascript(script)
    return "Created local note in Apple Notes."


def _create_reminder(payload: dict) -> str:
    title = _apple_string(payload["title"])
    notes = _apple_string(payload.get("notes", ""))
    list_name = _apple_string(payload.get("list_name", "Reminders"))
    script = f'''
tell application "Reminders"
    activate
    set targetList to missing value
    try
        set targetList to list "{list_name}"
    end try
    if targetList is missing value then
        set targetList to default list
    end if
    make new reminder at targetList with properties {{name:"{title}", body:"{notes}"}}
end tell
'''
    _osascript(script)
    return "Created local reminder in Apple Reminders."


def register(mcp):
    prepare_note = globals()["prepare_local_note"]
    prepare_reminder = globals()["prepare_local_reminder"]
    confirm_action = globals()["confirm_local_app_action"]
    cancel_action = globals()["cancel_local_app_action"]

    @mcp.tool()
    def prepare_local_note(title: str, body: str = "", folder: str = "Notes") -> dict:
        """Stage a local Apple Notes note. Never creates until confirmed."""
        return prepare_note(title=title, body=body, folder=folder)

    @mcp.tool()
    def prepare_local_reminder(title: str, notes: str = "", list_name: str = "Reminders") -> dict:
        """Stage a local Apple Reminders item. Never creates until confirmed."""
        return prepare_reminder(title=title, notes=notes, list_name=list_name)

    @mcp.tool()
    def confirm_local_app_action(action_id: str | None = None) -> dict:
        """Execute newest pending local Mac app action, or action_id."""
        return confirm_action(action_id=action_id)

    @mcp.tool()
    def cancel_local_app_action(action_id: str | None = None) -> dict:
        """Cancel pending local Mac app action."""
        return cancel_action(action_id=action_id)
