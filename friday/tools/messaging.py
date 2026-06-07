"""
Messaging tools with an explicit confirmation broker for risky sends.
"""

from __future__ import annotations

import os
import re
import subprocess
import time
import uuid
import webbrowser
from dataclasses import dataclass
from urllib.parse import quote

from dotenv import load_dotenv

from friday.tools.desktop import _reject_secret_content


load_dotenv()

PENDING_TTL_SECONDS = 600
SUPPORTED_CHANNELS = {"messages", "imessage", "sms", "whatsapp", "slack", "email", "mail"}
PENDING_ACTIONS: dict[str, "PendingAction"] = {}


@dataclass
class PendingAction:
    action_id: str
    summary: str
    risk: str
    created_at: float
    payload: dict


def clear_pending_actions() -> None:
    PENDING_ACTIONS.clear()


def _normalize_channel(channel: str) -> str:
    normalized = channel.strip().lower()
    if normalized not in SUPPORTED_CHANNELS:
        raise ValueError(f"Unsupported message channel: {channel}")
    if normalized in {"imessage", "sms"}:
        return "messages"
    if normalized == "mail":
        return "email"
    return normalized


def _validate_message(recipient: str, message: str) -> tuple[str, str]:
    clean_recipient = recipient.strip()
    clean_message = message.strip()
    if not clean_recipient:
        raise ValueError("Recipient is required.")
    if not clean_message:
        raise ValueError("Message is required.")
    _reject_secret_content(clean_recipient)
    _reject_secret_content(clean_message)
    return clean_recipient, clean_message


def _apple_string(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


def _osascript(script: str) -> subprocess.CompletedProcess:
    return subprocess.run(["osascript", "-e", script], check=True)


def _osascript_output(script: str) -> str:
    result = subprocess.run(
        ["osascript", "-e", script],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _prune_expired() -> None:
    now = time.time()
    expired = [
        action_id
        for action_id, action in PENDING_ACTIONS.items()
        if now - action.created_at > PENDING_TTL_SECONDS
    ]
    for action_id in expired:
        PENDING_ACTIONS.pop(action_id, None)


def prepare_outbound_message(channel: str, recipient: str, message: str) -> dict:
    normalized_channel = _normalize_channel(channel)
    clean_recipient, clean_message = _validate_message(recipient, message)
    resolved_recipient = None
    delivery_recipient = clean_recipient
    delivery_method = None

    if normalized_channel in {"messages", "whatsapp"} and not _looks_like_direct_handle(clean_recipient):
        matches = _find_contact_matches(clean_recipient)
        selectable_matches = matches
        if normalized_channel == "whatsapp":
            selectable_matches = [item for item in matches if item["kind"] == "phone"]

        selected = _select_contact_match(clean_recipient, selectable_matches)
        if selected is None:
            if selectable_matches:
                return {
                    "status": "needs_recipient_clarification",
                    "recipient": clean_recipient,
                    "matches": selectable_matches[:5],
                    "message": "Multiple contacts matched. Ask the user which contact to use.",
                }
            if normalized_channel == "whatsapp":
                delivery_method = "whatsapp_app_search"
            else:
                return {
                    "status": "contact_not_found",
                    "recipient": clean_recipient,
                    "matches": [],
                    "message": "No matching contact found. Ask for a phone number, email, or more specific contact name.",
                }
        else:
            resolved_recipient = selected
            delivery_recipient = selected["handle"]

    if normalized_channel == "whatsapp" and delivery_method is None:
        delivery_method = "whatsapp_url" if _digits_and_plus(delivery_recipient) else "whatsapp_app_search"

    if normalized_channel == "messages" and not _looks_like_direct_handle(clean_recipient) and resolved_recipient is None:
        return {
            "status": "contact_not_found",
            "recipient": clean_recipient,
            "matches": [],
            "message": "No matching contact found. Ask for a phone number, email, or more specific contact name.",
        }

    action_id = uuid.uuid4().hex
    recipient_summary = clean_recipient
    if resolved_recipient is not None:
        label = resolved_recipient.get("label") or resolved_recipient.get("kind") or "contact"
        recipient_summary = f"{resolved_recipient['name']} ({label}: {resolved_recipient['handle']})"
    summary = f"Send {normalized_channel} message to {recipient_summary}: {clean_message}"
    action = PendingAction(
        action_id=action_id,
        summary=summary,
        risk="send_message",
        created_at=time.time(),
        payload={
            "channel": normalized_channel,
            "recipient": delivery_recipient,
            "requested_recipient": clean_recipient,
            "resolved_recipient": resolved_recipient,
            "delivery_method": delivery_method,
            "message": clean_message,
        },
    )
    PENDING_ACTIONS[action_id] = action
    result = {
        "status": "pending_confirmation",
        "action_id": action_id,
        "risk": action.risk,
        "summary": summary,
        "expires_in_seconds": PENDING_TTL_SECONDS,
    }
    if resolved_recipient is not None:
        result["resolved_recipient"] = resolved_recipient
    return result


def _latest_pending_action_id() -> str:
    _prune_expired()
    if not PENDING_ACTIONS:
        raise KeyError("No pending message action found.")
    return max(PENDING_ACTIONS.values(), key=lambda action: action.created_at).action_id


def confirm_pending_action(action_id: str | None = None) -> dict:
    if not action_id:
        action_id = _latest_pending_action_id()

    action = PENDING_ACTIONS.get(action_id)
    if action is None:
        raise KeyError(f"No pending action found: {action_id}")

    if time.time() - action.created_at > PENDING_TTL_SECONDS:
        PENDING_ACTIONS.pop(action_id, None)
        raise TimeoutError(f"Pending action expired: {action_id}")

    _prune_expired()
    result = _execute_message_action(action.payload)
    PENDING_ACTIONS.pop(action_id, None)
    return {
        "status": "executed",
        "action_id": action_id,
        "summary": action.summary,
        "result": result,
    }


def _execute_message_action(payload: dict) -> str:
    channel = payload["channel"]
    recipient = payload["recipient"]
    message = payload["message"]
    if channel == "messages":
        return _send_apple_message(recipient, message)
    if channel == "whatsapp":
        if payload.get("delivery_method") == "whatsapp_app_search":
            return _send_whatsapp_app_contact(payload.get("requested_recipient") or recipient, message)
        return _send_whatsapp_url_message(recipient, message)
    if channel == "slack":
        return _open_slack_draft(recipient, message)
    if channel == "email":
        return _open_email_draft(recipient, message)
    raise ValueError(f"Unsupported message channel: {channel}")


def _looks_like_direct_handle(recipient: str) -> bool:
    return "@" in recipient or bool(_digits_and_plus(recipient))


def _contact_row(name: str, kind: str, handle: str, label: str) -> dict:
    return {
        "name": name.strip(),
        "kind": kind.strip() or "contact",
        "handle": handle.strip(),
        "label": label.strip(),
    }


def _parse_contact_rows(raw: str) -> list[dict]:
    matches = []
    seen = set()
    for line in raw.splitlines():
        parts = line.split("\t")
        if len(parts) != 4:
            continue
        item = _contact_row(parts[0], parts[1], parts[2], parts[3])
        if not item["name"] or not item["handle"]:
            continue
        key = (item["name"].lower(), item["handle"].lower())
        if key in seen:
            continue
        seen.add(key)
        matches.append(item)
    return matches


def _find_contact_matches(recipient: str) -> list[dict]:
    query = recipient.strip()
    if not query:
        return []
    script = f'''
set targetName to "{_apple_string(query)}"
set outputRows to {{}}
set oldDelimiters to AppleScript's text item delimiters

tell application "Contacts"
    set matches to every person whose name contains targetName
    repeat with targetPerson in matches
        set contactName to (name of targetPerson) as text

        repeat with targetPhone in phones of targetPerson
            set phoneLabel to ""
            try
                set phoneLabel to (label of targetPhone) as text
            end try
            set phoneValue to (value of targetPhone) as text
            copy (contactName & tab & "phone" & tab & phoneValue & tab & phoneLabel) to end of outputRows
        end repeat

        repeat with targetEmail in emails of targetPerson
            set emailLabel to ""
            try
                set emailLabel to (label of targetEmail) as text
            end try
            set emailValue to (value of targetEmail) as text
            copy (contactName & tab & "email" & tab & emailValue & tab & emailLabel) to end of outputRows
        end repeat
    end repeat
end tell

set AppleScript's text item delimiters to linefeed
set outputText to outputRows as text
set AppleScript's text item delimiters to oldDelimiters
return outputText
'''
    try:
        return _parse_contact_rows(_osascript_output(script))
    except Exception:
        return []


def _select_contact_match(query: str, matches: list[dict]) -> dict | None:
    if len(matches) == 1:
        return matches[0]
    if not matches:
        return None

    normalized_query = query.strip().lower()
    exact = [item for item in matches if item["name"].strip().lower() == normalized_query]
    if len(exact) == 1:
        return exact[0]

    mobile = [
        item for item in matches
        if item["kind"] == "phone" and item.get("label", "").strip().lower() in {"mobile", "iphone"}
    ]
    unique_names = {item["name"].strip().lower() for item in matches}
    if len(unique_names) == 1 and len(mobile) == 1:
        return mobile[0]

    return None


def _lookup_contact_handle(recipient: str) -> str | None:
    match = _select_contact_match(recipient, _find_contact_matches(recipient))
    if match:
        return match["handle"]
    return None


def _send_apple_message(recipient: str, message: str) -> str:
    handle = recipient
    if not _looks_like_direct_handle(recipient):
        handle = _lookup_contact_handle(recipient) or recipient
    return _send_messages_handle(handle, message)


def _send_messages_handle(recipient: str, message: str) -> str:
    errors = []
    for service_type, label in (("iMessage", "iMessage"), ("SMS", "SMS")):
        script = _messages_send_script(service_type, recipient, message)
        try:
            _osascript(script)
            return f"Sent through Apple Messages ({label})."
        except Exception as exc:
            errors.append(f"{label}: {exc}")

    return _open_messages_draft(recipient, message, errors)


def _messages_send_script(service_type: str, recipient: str, message: str) -> str:
    script = f'''
tell application "Messages"
    set targetBuddy to "{_apple_string(recipient)}"
    set targetService to 1st service whose service type = {service_type}
    send "{_apple_string(message)}" to buddy targetBuddy of targetService
end tell
'''
    return script


def _open_messages_draft(recipient: str, message: str, errors: list[str]) -> str:
    url = f"sms:{quote(recipient, safe='+@')}&body={quote(message)}"
    webbrowser.open(url)
    error_text = "; ".join(errors)[:240]
    return (
        "Opened Messages draft because direct send failed. "
        "Review and press Send in Messages. "
        f"Direct-send error: {error_text}"
    )


def _digits_and_plus(value: str) -> str:
    cleaned = re.sub(r"[^\d+]", "", value)
    return cleaned


def _send_whatsapp_url_message(recipient: str, message: str) -> str:
    phone = _digits_and_plus(recipient)
    encoded = quote(message)
    if phone:
        url = f"whatsapp://send?phone={quote(phone)}&text={encoded}"
    else:
        url = f"whatsapp://send?text={encoded}"
    webbrowser.open(url)
    try:
        _whatsapp_press_send()
        return "Sent through WhatsApp."
    except Exception as exc:
        return f"Opened WhatsApp draft. Review and send from WhatsApp. Direct-send error: {exc}"


def _whatsapp_press_send() -> None:
    delay = float(os.getenv("FRIDAY_WHATSAPP_SEND_DELAY", "1.2"))
    script = f'''
delay {delay}
tell application "WhatsApp" to activate
delay 0.2
tell application "System Events" to key code 36
'''
    _osascript(script)


def _send_whatsapp_app_contact(recipient: str, message: str) -> str:
    script = f'''
tell application "WhatsApp" to activate
delay 0.8
tell application "System Events"
    keystroke "n" using {{command down}}
    delay 0.4
    keystroke "{_apple_string(recipient)}"
    delay 0.7
    key code 36
    delay 0.8
    keystroke "{_apple_string(message)}"
    delay 0.2
    key code 36
end tell
'''
    _osascript(script)
    return "Sent through WhatsApp app search."


def _open_slack_draft(recipient: str, message: str) -> str:
    query = quote(f"{recipient} {message}")
    webbrowser.open(f"slack://search/{query}")
    return "Opened Slack draft/search target. Review and send from Slack."


def _open_email_draft(recipient: str, message: str) -> str:
    subject = quote(os.getenv("FRIDAY_EMAIL_SUBJECT", "Message from FRIDAY"))
    body = quote(message)
    webbrowser.open(f"mailto:{quote(recipient)}?subject={subject}&body={body}")
    return "Opened email draft. Review and send from Mail."


def register(mcp):
    @mcp.tool()
    def prepare_message(channel: str, recipient: str, message: str) -> dict:
        """Prepare a message for confirmation. This never sends immediately."""
        return prepare_outbound_message(channel=channel, recipient=recipient, message=message)

    @mcp.tool()
    def confirm_message_action(action_id: str | None = None) -> dict:
        """Execute a previously prepared message after explicit confirmation. If action_id is omitted, executes the newest pending message."""
        return confirm_pending_action(action_id=action_id)
