"""Spotify MCP tools — Web API search + AppleScript playback.

No user OAuth. Uses Spotify's client-credentials flow to mint a server
token (free, no user consent needed) for **search**, then drives the
local desktop Spotify app via AppleScript for **playback**. This works
on any macOS Spotify account (free or Premium) as long as the desktop
app is installed.

Env:

* ``SPOTIFY_CLIENT_ID`` — required for search (register a free app at
  https://developer.spotify.com/dashboard).
* ``SPOTIFY_CLIENT_SECRET`` — required for search.
* Neither is required for play/pause/next/prev/volume — those only
  need the Spotify desktop app installed and logged in.

Capabilities:

* Search tracks / artists / albums / playlists by name → returns URI.
* Play a track / artist / album / playlist by name.
* Play a raw Spotify URI directly.
* Pause / resume / next / previous.
* Volume 0-100.
* Now playing (via AppleScript — reads what the desktop app is
  currently displaying).

Security notes:

* No user tokens stored. The server token is cached in memory and
  refreshed on expiry.
* AppleScript calls go through ``subprocess.run`` with argv lists, not
  ``shell=True``.
* Track URI inputs are validated against the Spotify URI grammar
  before being passed to AppleScript.
"""

from __future__ import annotations

import base64
import os
import re
import subprocess
import time

import httpx
from dotenv import load_dotenv


load_dotenv()


TOKEN_URL = "https://accounts.spotify.com/api/token"
API_BASE = "https://api.spotify.com/v1"

_URI_RE = re.compile(r"^spotify:(track|artist|album|playlist|episode|show):[A-Za-z0-9]{16,40}$")

_token_cache: dict = {"access_token": None, "expires_at": 0.0}


# ---------------------------------------------------------------------
# Client credentials token
# ---------------------------------------------------------------------

def _credentials() -> tuple[str, str]:
    cid = os.getenv("SPOTIFY_CLIENT_ID", "").strip()
    secret = os.getenv("SPOTIFY_CLIENT_SECRET", "").strip()
    if not cid or not secret:
        raise RuntimeError(
            "SPOTIFY_CLIENT_ID and SPOTIFY_CLIENT_SECRET aren't set. "
            "Register a free app at https://developer.spotify.com/dashboard "
            "and put both in .env."
        )
    return cid, secret


def _bearer() -> str:
    if _token_cache["access_token"] and time.time() < _token_cache["expires_at"] - 30:
        return _token_cache["access_token"]
    cid, secret = _credentials()
    basic = base64.b64encode(f"{cid}:{secret}".encode()).decode()
    resp = httpx.post(
        TOKEN_URL,
        data={"grant_type": "client_credentials"},
        headers={"Authorization": f"Basic {basic}"},
        timeout=10,
    )
    resp.raise_for_status()
    payload = resp.json()
    _token_cache["access_token"] = payload["access_token"]
    _token_cache["expires_at"] = time.time() + int(payload.get("expires_in", 3600))
    return _token_cache["access_token"]


# ---------------------------------------------------------------------
# Web API search
# ---------------------------------------------------------------------

def _search(query: str, kind: str) -> dict | None:
    if not query.strip():
        raise ValueError("query is required")
    if kind not in {"track", "artist", "album", "playlist"}:
        raise ValueError("kind must be one of: track, artist, album, playlist")
    headers = {"Authorization": f"Bearer {_bearer()}"}
    resp = httpx.get(
        f"{API_BASE}/search",
        params={"q": query, "type": kind, "limit": 1},
        headers=headers,
        timeout=10,
    )
    resp.raise_for_status()
    items = ((resp.json() or {}).get(f"{kind}s") or {}).get("items") or []
    return items[0] if items else None


# ---------------------------------------------------------------------
# AppleScript driver
# ---------------------------------------------------------------------

class _AppleScriptError(RuntimeError):
    pass


def _osa(script: str) -> str:
    """Run an osascript snippet, return stdout or raise."""
    try:
        result = subprocess.run(
            ["osascript", "-e", script],
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except FileNotFoundError as exc:
        raise _AppleScriptError(f"osascript not found: {exc}") from exc
    except subprocess.TimeoutExpired as exc:
        raise _AppleScriptError(f"osascript timed out: {exc}") from exc
    if result.returncode != 0:
        msg = (result.stderr or result.stdout).strip()
        # Common case: the Spotify app isn't running. The agent should
        # know to suggest opening it.
        raise _AppleScriptError(f"osascript error: {msg or 'unknown'}")
    return result.stdout.strip()


def _ensure_spotify_running() -> None:
    """Activate the Spotify app — launches it if not running."""
    _osa('tell application "Spotify" to activate')


def _validate_uri(uri: str) -> str:
    uri = uri.strip()
    if not _URI_RE.match(uri):
        raise ValueError(
            "Invalid Spotify URI. Expected something like "
            "spotify:track:6rqhFgbbKwnb9MLmUQDhG6"
        )
    return uri


def _osa_play_uri(uri: str) -> None:
    safe = _validate_uri(uri)
    _ensure_spotify_running()
    # Tiny wait so the app responds to a `play track` right after launch.
    time.sleep(0.4)
    _osa(f'tell application "Spotify" to play track "{safe}"')


def _osa_now_playing() -> dict:
    try:
        track = _osa('tell application "Spotify" to name of current track as string')
        artist = _osa('tell application "Spotify" to artist of current track as string')
        album = _osa('tell application "Spotify" to album of current track as string')
        state = _osa('tell application "Spotify" to player state as string')
        position = _osa('tell application "Spotify" to player position as string')
        duration = _osa(
            'tell application "Spotify" to duration of current track as string'
        )
    except _AppleScriptError as exc:
        return {"status": "unavailable", "error": str(exc)}
    return {
        "status": state.lower(),
        "track": track,
        "artist": artist,
        "album": album,
        "position_s": _safe_float(position),
        "duration_ms": _safe_int(duration),
    }


def _safe_float(value: str) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _safe_int(value: str) -> int | None:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------
# Public tool surface
# ---------------------------------------------------------------------

def _play_by_search(query: str, kind: str) -> dict:
    item = _search(query, kind)
    if not item:
        return {"status": "not_found", "query": query, "kind": kind}
    uri = item.get("uri")
    if not uri:
        return {"status": "no_uri", "query": query, "kind": kind}
    _osa_play_uri(uri)
    return {
        "status": "playing",
        "kind": kind,
        "uri": uri,
        "name": item.get("name"),
        "artists": ", ".join(a.get("name", "") for a in item.get("artists") or []) if kind != "playlist" else None,
    }


def register(mcp):

    @mcp.tool()
    def spotify_play_track(query: str) -> dict:
        """
        Search for a track by name (and optionally artist) and play it
        on the local Spotify app. Examples: "blinding lights",
        "redbone childish gambino", "thriller michael jackson".
        """
        return _play_by_search(query, "track")

    @mcp.tool()
    def spotify_play_artist(query: str) -> dict:
        """Search for an artist and play their top tracks / artist radio."""
        return _play_by_search(query, "artist")

    @mcp.tool()
    def spotify_play_album(query: str) -> dict:
        """Search for an album by name and play it from track 1."""
        return _play_by_search(query, "album")

    @mcp.tool()
    def spotify_play_playlist(query: str) -> dict:
        """Search for a playlist by name and start it."""
        return _play_by_search(query, "playlist")

    @mcp.tool()
    def spotify_play_uri(uri: str) -> dict:
        """
        Play a raw Spotify URI directly — e.g.
        "spotify:track:6rqhFgbbKwnb9MLmUQDhG6". Use when you already
        know the URI from a previous search and want to skip search.
        """
        safe = _validate_uri(uri)
        _osa_play_uri(safe)
        return {"status": "playing", "uri": safe}

    @mcp.tool()
    def spotify_pause() -> dict:
        """Pause Spotify playback."""
        _osa('tell application "Spotify" to pause')
        return {"status": "paused"}

    @mcp.tool()
    def spotify_resume() -> dict:
        """Resume playback from where it was paused."""
        _ensure_spotify_running()
        _osa('tell application "Spotify" to play')
        return {"status": "resumed"}

    @mcp.tool()
    def spotify_next_track() -> dict:
        """Skip to the next track."""
        _osa('tell application "Spotify" to next track')
        return {"status": "skipped"}

    @mcp.tool()
    def spotify_previous_track() -> dict:
        """Go back to the previous track."""
        _osa('tell application "Spotify" to previous track')
        return {"status": "rewound"}

    @mcp.tool()
    def spotify_set_volume(percent: int) -> dict:
        """Set Spotify volume 0-100."""
        try:
            level = max(0, min(100, int(percent)))
        except (TypeError, ValueError):
            raise ValueError("percent must be an integer 0-100")
        _osa(f'tell application "Spotify" to set sound volume to {level}')
        return {"status": "ok", "volume": level}

    @mcp.tool()
    def spotify_now_playing() -> dict:
        """What's currently playing on the local Spotify app?"""
        return _osa_now_playing()

    @mcp.tool()
    def spotify_search(query: str, kind: str = "track") -> dict:
        """
        Search Spotify by ``query`` and return the top hit (no playback).
        ``kind`` is one of: track | artist | album | playlist.
        """
        item = _search(query, kind)
        if not item:
            return {"status": "not_found", "query": query, "kind": kind}
        return {
            "status": "found",
            "kind": kind,
            "uri": item.get("uri"),
            "name": item.get("name"),
            "artists": ", ".join(a.get("name", "") for a in item.get("artists") or []) if kind != "playlist" else None,
            "external_url": (item.get("external_urls") or {}).get("spotify"),
        }
