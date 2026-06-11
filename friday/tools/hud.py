"""HUD MCP tools — panel control + camera vision.

Panel switches are direct and reversible (same trust level as the
Odysseus open/close tools): they append ``hud_panel`` events to the
shared desktop event log and the HUD's 300 ms poller routes them.

``describe_camera_view`` is the cross-process snapshot dance: this tool
(running in the MCP server or the desktop Brain) writes a
``{"type": "camera", "action": "snapshot", "path": …}`` event; the
desktop CameraPanel captures a frame to that path (starting the camera
if needed, stopping it after); the tool waits for the file, then runs
OpenAI vision on it. If the desktop HUD isn't running, it times out with
an honest error instead of pretending.
"""

from __future__ import annotations

import base64
import os
import time
import uuid
from pathlib import Path


HUD_PANELS = ("core", "camera", "ops")
DEFAULT_SNAPSHOT_TIMEOUT_S = 15.0
DEFAULT_VISION_MODEL = "gpt-4o"


def _snapshot_dir() -> Path:
    folder = Path(os.getenv("FRIDAY_CAMERA_SNAPSHOT_DIR", "/tmp/friday-camera"))
    folder.mkdir(parents=True, exist_ok=True)
    return folder


def _snapshot_timeout() -> float:
    return float(os.getenv("FRIDAY_CAMERA_SNAPSHOT_TIMEOUT_S", DEFAULT_SNAPSHOT_TIMEOUT_S))


def _emit(event_type: str, **payload) -> bool:
    try:
        from friday.desktop.events import append_event

        append_event(event_type, source="hud_tools", **payload)
        return True
    except Exception:
        return False


def show_hud_panel(panel: str) -> dict:
    panel = (panel or "").strip().lower()
    if panel not in HUD_PANELS:
        raise ValueError(f"Unknown HUD panel {panel!r}. Allowed: {HUD_PANELS}")
    ok = _emit("hud_panel", panel=panel)
    return {
        "status": "ok" if ok else "error",
        "panel": panel,
        "note": None if ok else "could not write the desktop event log",
    }


def request_camera_snapshot(timeout_s: float | None = None) -> str | None:
    """Ask the desktop HUD for a camera frame. Returns the image path or
    None if the HUD never delivered (not running / camera missing)."""
    target = _snapshot_dir() / f"cam-{uuid.uuid4().hex}.jpg"
    if not _emit("camera", action="snapshot", path=str(target)):
        return None
    deadline = time.monotonic() + (timeout_s if timeout_s is not None else _snapshot_timeout())
    last_size = -1
    while time.monotonic() < deadline:
        if target.is_file():
            size = target.stat().st_size
            if size > 0 and size == last_size:
                return str(target)  # finished writing (size stable)
            last_size = size
        time.sleep(0.25)
    return None


def _image_data_url(path: str) -> str:
    data = Path(path).read_bytes()
    return "data:image/jpeg;base64," + base64.b64encode(data).decode("ascii")


def describe_camera(prompt: str = "", client=None) -> dict:
    path = request_camera_snapshot()
    if path is None:
        return {
            "status": "error",
            "error": (
                "No frame came back from the camera — the desktop HUD isn't "
                "running, the camera is missing, or permission was denied."
            ),
        }
    request = prompt.strip() or (
        "Describe what you see through this camera: people, objects, "
        "surroundings, anything notable. Be concise and factual. Do not "
        "guess identities or read private documents."
    )
    if client is None:
        from openai import OpenAI

        client = OpenAI()
    response = client.responses.create(
        model=os.getenv("OPENAI_VISION_MODEL", DEFAULT_VISION_MODEL),
        input=[{
            "role": "user",
            "content": [
                {"type": "input_text", "text": request},
                {"type": "input_image", "image_url": _image_data_url(path)},
            ],
        }],
    )
    return {"status": "ok", "snapshot_path": path, "description": response.output_text}


def register(mcp):
    @mcp.tool()
    def open_hud_panel(panel: str) -> dict:
        """
        Switch the desktop HUD center view. panel: "camera" (live camera
        feed), "ops" (learning/agents/security board), or "core" (orb).
        Direct and reversible — no confirmation needed.
        """
        return show_hud_panel(panel)

    @mcp.tool()
    def close_hud_panel() -> dict:
        """Return the desktop HUD to the core orb view."""
        return show_hud_panel("core")

    @mcp.tool()
    def describe_camera_view(prompt: str = "") -> dict:
        """
        Look through the Mac's camera and describe what's visible ("what
        do you see?", "look at this"). Grabs one frame via the HUD (camera
        turns off again afterwards) and runs vision on it. Requires the
        desktop HUD to be running.
        """
        return describe_camera(prompt=prompt)
